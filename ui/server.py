#!/usr/bin/env python3
"""Local test UI for cloudcleaner — drives the real code paths against a
single, hardcoded sandbox bucket.

SAFETY (read before changing):
  * The bucket is HARDCODED (BUCKET below). No request can point cloudcleaner
    at a different bucket — the value never comes from the client.
  * The server binds to 127.0.0.1 only. This UI can delete and repopulate S3
    objects, so it must never be reachable off the local machine.
  * Every mutating action maps to a real cloudcleaner call, so the UI is a
    faithful demonstration, not a mock.

Run:  .venv/bin/python ui/server.py   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Make the package importable when run from the repo root or ui/.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import boto3  # noqa: E402  (the venv has it)

from cloudcleaner.adapters.s3 import S3Adapter  # noqa: E402
from cloudcleaner.config import (  # noqa: E402
    Config,
    QuarantineSettings,
    Rule,
)
from cloudcleaner.dedup import choose_redundant, find_duplicates, total_reclaimable  # noqa: E402
from cloudcleaner.multipart import abort_uploads, find_incomplete_uploads  # noqa: E402
from cloudcleaner.quarantine import QuarantineManager  # noqa: E402
from cloudcleaner.report import compute_savings  # noqa: E402
from cloudcleaner.rules import ExclusionPolicy, RuleEngine  # noqa: E402

# ---- hardcoded, non-overridable target -------------------------------------
BUCKET = "cloudcleaner-ui-benjnat"
REGION = "us-east-1"
HOST, PORT = "127.0.0.1", 8765
QUARANTINE_PREFIX = "_cloudcleaner/quarantine/"
GIB = 1024**3


def _config() -> Config:
    """The rule set the UI demonstrates.

    Uses a mix of condition kinds so every feature has something to match:
    keyword (logs), suffix (temp artifacts), prefix (old invoices),
    boolean composition, and keep_newest (backups). legal-hold/ is excluded
    and must survive everything.
    """
    return Config(
        provider="s3",
        bucket=BUCKET,
        region=REGION,
        rules=[
            Rule(name="stale-logs", keywords=["log"]),
            Rule(name="temp-artifacts", suffixes=[".tmp", ".bak", ".old"]),
            Rule(name="old-invoices", prefixes=["invoices/2010/"]),
            Rule(
                name="keep-3-newest-backups",
                prefixes=["backups/db/"],
                keep_newest=3,
                group_by="prefix:2",
            ),
        ],
        exclude=["legal-hold/"],
        quarantine=QuarantineSettings(
            prefix=QUARANTINE_PREFIX,
            retention_days=30,
            guardrail={"max_fraction": 0.9},
        ),
    )


def _adapter() -> S3Adapter:
    return S3Adapter(region=REGION)


def _client():
    return boto3.client("s3", region_name=REGION)


def _dedupable(adapter, config):
    """Objects eligible for dedup: excludes legal-hold/ and the quarantine
    prefix, mirroring the rule engine's protections."""
    exclusions = ExclusionPolicy(config.exclude, config.quarantine.prefix)
    return [o for o in adapter.list_objects(BUCKET) if not exclusions.is_excluded(o.key)]


# ---- seed scenario ---------------------------------------------------------
def seed_bucket() -> dict:
    """Wipe the bucket clean and repopulate a rich test scenario."""
    c = _client()
    # 1. delete every object
    paginator = c.get_paginator("list_objects_v2")
    to_delete = []
    for page in paginator.paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            to_delete.append({"Key": item["Key"]})
    for i in range(0, len(to_delete), 1000):
        c.delete_objects(Bucket=BUCKET, Delete={"Objects": to_delete[i : i + 1000]})
    # 2. abort any lingering multipart uploads
    mpu = c.list_multipart_uploads(Bucket=BUCKET)
    for u in mpu.get("Uploads", []):
        c.abort_multipart_upload(Bucket=BUCKET, Key=u["Key"], UploadId=u["UploadId"])

    def put(key: str, body: bytes):
        c.put_object(Bucket=BUCKET, Key=key, Body=body)

    # stale logs (keyword: log)
    for i in range(6):
        put(f"logs/app/app-{i:03}.log", b"log line\n" * (i + 1))
    # temp artifacts (suffixes)
    for i in range(4):
        put(f"build/cache-{i}.tmp", b"x" * 2048)
    put("build/last.bak", b"backup blob")
    # old invoices (prefix)
    for i in range(5):
        put(f"invoices/2010/invoice-{i:04}.pdf", b"%PDF-1.4 old invoice")
    put("invoices/2025/invoice-9999.pdf", b"%PDF-1.4 recent invoice")  # NOT a candidate
    # backups for keep_newest (5 -> 2 oldest become candidates)
    for i in range(5):
        put(f"backups/db/full-{i:02}.dump", b"D" * (4096 * (i + 1)))
    # exact duplicates: same content in 3 places
    dup = b"IDENTICAL DUPLICATE PAYLOAD " * 64
    put("data/report-copy-a.bin", dup)
    put("archive/report-copy-b.bin", dup)
    put("backup/report-copy-c.bin", dup)
    # protected — must survive everything
    for i in range(3):
        put(f"legal-hold/case-42/evidence-{i}.zip", b"sealed evidence")
    # an incomplete multipart upload (orphaned parts, invisible in listings)
    c.create_multipart_upload(Bucket=BUCKET, Key="uploads/interrupted-huge.bin")

    return get_state()


# ---- read state ------------------------------------------------------------
RULE_OF_PREFIX = _config()


def _classify(key: str) -> str:
    if key.startswith(QUARANTINE_PREFIX):
        return "quarantine"
    if key.startswith("legal-hold/"):
        return "protected"
    return "live"


def get_state() -> dict:
    adapter = _adapter()
    config = _config()
    objects = list(adapter.list_objects(BUCKET))

    # scan (candidates + savings) via the real engine
    engine = RuleEngine(config)
    result = engine.scan(o for o in objects if not o.key.startswith(QUARANTINE_PREFIX))
    savings = compute_savings(result, config.pricing)
    candidate_keys = {c.obj.key: c.rule_name for c in result.candidates}

    # group live objects by top-level prefix for the visualization
    groups: dict[str, dict] = {}
    live_objs = 0
    live_bytes = 0
    for o in objects:
        cls = _classify(o.key)
        top = o.key.split("/", 1)[0] + "/" if "/" in o.key else o.key
        g = groups.setdefault(top, {"prefix": top, "objects": 0, "bytes": 0, "candidates": 0})
        g["objects"] += 1
        g["bytes"] += o.size_bytes
        if o.key in candidate_keys:
            g["candidates"] += 1
        if cls == "live":
            live_objs += 1
            live_bytes += o.size_bytes

    # duplicates (respecting exclusions + quarantine prefix)
    dup_groups = find_duplicates(_dedupable(adapter, config))
    dup_reclaimable = total_reclaimable(dup_groups)

    # multipart
    uploads = find_incomplete_uploads(adapter, BUCKET)

    # quarantine batches + audit log
    manager = QuarantineManager(adapter, config)
    batches = [
        {
            "batch_id": m.batch_id,
            "objects": len(m.entries),
            "bytes": m.total_bytes,
            "purge_after": m.purge_after,
            "expired": m.is_expired(manager.now),
        }
        for m in manager.list_batches()
    ]
    audit = manager.read_audit_log()

    return {
        "bucket": BUCKET,
        "totals": {
            "objects": len(objects),
            "bytes": sum(o.size_bytes for o in objects),
            "live_objects": live_objs,
            "live_bytes": live_bytes,
            "candidates": result.candidate_count,
            "candidate_bytes": result.candidate_bytes,
            "duplicate_groups": len(dup_groups),
            "duplicate_reclaimable": dup_reclaimable,
            "incomplete_uploads": len(uploads),
        },
        "savings": {
            "currency": savings.currency,
            "current_monthly": round(savings.current_monthly, 4),
            "after_monthly": round(savings.after_monthly, 4),
            "monthly": round(savings.monthly, 4),
            "yearly": round(savings.yearly, 4),
        },
        "groups": sorted(groups.values(), key=lambda g: -g["bytes"]),
        "candidates": [
            {"key": c.obj.key, "bytes": c.obj.size_bytes, "rule": c.rule_name}
            for c in result.candidates
        ],
        "duplicates": [
            {
                "etag": g.etag,
                "size_bytes": g.size_bytes,
                "count": g.count,
                "reclaimable": g.redundant_bytes,
                "keys": [m.key for m in g.members],
                "redundant": [m.key for m in choose_redundant(g, keep="oldest")],
            }
            for g in dup_groups
        ],
        "uploads": [
            {"key": u.key, "upload_id": u.upload_id,
             "initiated": u.initiated.isoformat() if u.initiated else None,
             "size_bytes": u.size_bytes}
            for u in uploads
        ],
        "batches": batches,
        "audit": audit,
    }


# ---- actions ---------------------------------------------------------------
def action_quarantine(apply: bool, force: bool) -> dict:
    adapter = _adapter()
    config = _config()
    engine = RuleEngine(config)
    result = engine.scan(adapter.list_objects(BUCKET))
    if not result.candidates:
        return {"message": "No candidates matched the rules.", "state": get_state()}
    if not apply:
        return {
            "message": f"Dry run: {result.candidate_count} object(s) would be quarantined.",
            "state": get_state(),
        }
    # guardrail
    from cloudcleaner.guardrail import GuardrailLimits, GuardrailViolation, check_guardrail

    if not force:
        try:
            check_guardrail(result, GuardrailLimits.from_dict(config.quarantine.guardrail))
        except GuardrailViolation as exc:
            return {"error": f"guardrail: {exc}", "state": get_state()}
    manager = QuarantineManager(adapter, config)
    manifest = manager.quarantine(result.candidates)
    return {
        "message": f"Quarantined {len(manifest.entries)} object(s) as batch {manifest.batch_id}.",
        "state": get_state(),
    }


def action_purge(apply: bool, force: bool) -> dict:
    adapter = _adapter()
    config = _config()
    manager = QuarantineManager(adapter, config)
    batches = manager.list_batches() if force else manager.expired_batches()
    if not batches:
        return {"message": "No purgeable batches (retention window still open; use force).",
                "state": get_state()}
    if not apply:
        n = sum(len(m.entries) for m in batches)
        return {"message": f"Dry run: would permanently delete {n} object(s).", "state": get_state()}
    freed = manager.purge(batches)
    return {"message": f"Permanently deleted {freed} bytes across {len(batches)} batch(es).",
            "state": get_state()}


def action_restore() -> dict:
    adapter = _adapter()
    config = _config()
    manager = QuarantineManager(adapter, config)
    batches = manager.list_batches()
    if not batches:
        return {"message": "No quarantine batches to restore.", "state": get_state()}
    total = 0
    for m in batches:
        total += len(manager.restore(m.batch_id))
    return {"message": f"Restored {total} object(s) to their original keys.", "state": get_state()}


def action_dedup(apply: bool) -> dict:
    adapter = _adapter()
    config = _config()
    groups = find_duplicates(_dedupable(adapter, config))
    if not groups:
        return {"message": "No exact duplicates found.", "state": get_state()}
    redundant = [m.key for g in groups for m in choose_redundant(g, keep="oldest")]
    if not apply:
        return {"message": f"Dry run: {len(redundant)} redundant copy(ies) could be removed.",
                "state": get_state()}
    from cloudcleaner.bulk import bulk_delete

    bulk_delete(adapter, BUCKET, redundant)
    return {"message": f"Deleted {len(redundant)} redundant duplicate copy(ies).",
            "state": get_state()}


def action_multipart(apply: bool) -> dict:
    adapter = _adapter()
    uploads = find_incomplete_uploads(adapter, BUCKET)
    if not uploads:
        return {"message": "No incomplete multipart uploads.", "state": get_state()}
    if not apply:
        return {"message": f"Dry run: would abort {len(uploads)} incomplete upload(s).",
                "state": get_state()}
    count, reclaimed = abort_uploads(adapter, BUCKET, uploads)
    return {"message": f"Aborted {count} incomplete upload(s), reclaimed ~{reclaimed} bytes.",
            "state": get_state()}


ACTIONS = {
    "scan": lambda p: {"message": "Scan complete.", "state": get_state()},
    "quarantine": lambda p: action_quarantine(p.get("apply", False), p.get("force", False)),
    "purge": lambda p: action_purge(p.get("apply", False), p.get("force", False)),
    "restore": lambda p: action_restore(),
    "dedup": lambda p: action_dedup(p.get("apply", False)),
    "multipart": lambda p: action_multipart(p.get("apply", False)),
    "reset": lambda p: {"message": "Bucket reset and repopulated.", "state": seed_bucket()},
}


# ---- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path == "/api/state":
            try:
                return self._send(200, json.dumps(get_state()))
            except Exception as exc:  # noqa: BLE001
                return self._send(500, json.dumps({"error": str(exc)}))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self.path.startswith("/api/action/"):
            return self._send(404, json.dumps({"error": "not found"}))
        name = self.path[len("/api/action/") :]
        fn = ACTIONS.get(name)
        if not fn:
            return self._send(404, json.dumps({"error": f"unknown action {name!r}"}))
        length = int(self.headers.get("Content-Length", 0) or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
        try:
            return self._send(200, json.dumps(fn(payload)))
        except Exception as exc:  # noqa: BLE001
            return self._send(500, json.dumps({"error": str(exc)}))


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"cloudcleaner test UI on http://{HOST}:{PORT}  (bucket: {BUCKET})")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
