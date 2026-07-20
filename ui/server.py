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


# ---- large (multi-GB) seed for a visible-savings demo ----------------------
MiB = 1024 * 1024
SCRATCH = "_scratch/"


def _wipe(c):
    paginator = c.get_paginator("list_objects_v2")
    to_delete = []
    for page in paginator.paginate(Bucket=BUCKET):
        for item in page.get("Contents", []):
            to_delete.append({"Key": item["Key"]})
    for i in range(0, len(to_delete), 1000):
        c.delete_objects(Bucket=BUCKET, Delete={"Objects": to_delete[i : i + 1000]})
    for u in c.list_multipart_uploads(Bucket=BUCKET).get("Uploads", []):
        c.abort_multipart_upload(Bucket=BUCKET, Key=u["Key"], UploadId=u["UploadId"])


def _assemble(c, key, source_key, times):
    """Build `key` server-side by concatenating `times` copies of `source_key`
    via UploadPartCopy — no bandwidth, same-region copy is free. Part copies
    are independent, so run them concurrently (each 1 GiB copy is slow)."""
    from concurrent.futures import ThreadPoolExecutor

    uid = c.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]

    def copy_part(i):
        r = c.upload_part_copy(
            Bucket=BUCKET, Key=key, PartNumber=i, UploadId=uid,
            CopySource={"Bucket": BUCKET, "Key": source_key},
        )
        return {"ETag": r["CopyPartResult"]["ETag"], "PartNumber": i}

    with ThreadPoolExecutor(max_workers=16) as pool:
        parts = sorted(pool.map(copy_part, range(1, times + 1)), key=lambda p: p["PartNumber"])
    c.complete_multipart_upload(
        Bucket=BUCKET, Key=key, UploadId=uid, MultipartUpload={"Parts": parts}
    )


def seed_large() -> dict:
    """Wipe and seed a bucket with real multi-GB objects so dollar savings are
    visibly non-zero. Builds volume server-side from a 16 MiB seed via a size
    ladder (16 MiB -> 256 MiB -> 1 GiB), then assembles the big objects.

    Candidates total ~60 GiB (~$1.38/mo at S3 Standard list price). NOTE: this
    is real storage — reset (small seed) or delete the bucket when done.
    """
    c = _client()
    _wipe(c)

    # size ladder, built once in a scratch prefix, deleted at the end
    c.put_object(Bucket=BUCKET, Key=SCRATCH + "base16.bin", Body=b"0" * (16 * MiB))
    _assemble(c, SCRATCH + "chunk256.bin", SCRATCH + "base16.bin", 16)   # 256 MiB
    _assemble(c, SCRATCH + "chunk1g.bin", SCRATCH + "chunk256.bin", 4)   # 1 GiB
    chunk = SCRATCH + "chunk1g.bin"

    # Big objects, all built concurrently (independent multipart assemblies).
    # backups: keep_newest=3 -> 2 OLDEST are candidates (15 GiB each = 30 GiB);
    # newer 3 kept small. logs: keyword 'log' -> all candidates (3 x 10 GiB).
    from concurrent.futures import ThreadPoolExecutor

    big = [
        ("backups/db/full-00.dump", 15), ("backups/db/full-01.dump", 15),
        ("backups/db/full-02.dump", 2), ("backups/db/full-03.dump", 2),
        ("backups/db/full-04.dump", 2),
        ("logs/app/archive-0.log", 10), ("logs/app/archive-1.log", 10),
        ("logs/app/archive-2.log", 10),
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda t: _assemble(c, t[0], chunk, t[1]), big))

    # --- the rest of the feature demo, at small size ---
    def put(key, body):
        c.put_object(Bucket=BUCKET, Key=key, Body=body)

    for i in range(4):
        put(f"build/cache-{i}.tmp", b"x" * 2048)          # temp artifacts
    for i in range(5):
        put(f"invoices/2010/invoice-{i:04}.pdf", b"%PDF old")  # old invoices
    put("invoices/2025/invoice-9999.pdf", b"%PDF recent")      # NOT a candidate
    dup = b"IDENTICAL DUPLICATE PAYLOAD " * 64                  # exact duplicates
    put("data/report-copy-a.bin", dup)
    put("archive/report-copy-b.bin", dup)
    put("backup/report-copy-c.bin", dup)
    for i in range(3):
        put(f"legal-hold/case-42/evidence-{i}.zip", b"sealed")  # protected
    c.create_multipart_upload(Bucket=BUCKET, Key="uploads/interrupted-huge.bin")

    # remove the scratch ladder; the big objects are independent copies now
    _wipe_prefix(c, SCRATCH)
    return get_state()


def _wipe_prefix(c, prefix):
    keys = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for item in page.get("Contents", []):
            keys.append({"Key": item["Key"]})
    if keys:
        c.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})


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


# ---- clean steps -----------------------------------------------------------
# Each step is `fn(apply, opts) -> (message, is_error)` — it does the work but
# does NOT compute state; the endpoint attaches state once. This lets several
# steps compose into a single run without recomputing state per step.
def step_dedup(apply: bool, opts: dict) -> tuple[str, bool]:
    adapter = _adapter()
    config = _config()
    keep = opts.get("keep", "oldest")
    groups = find_duplicates(_dedupable(adapter, config), min_size=int(opts.get("min_size", 0) or 0))
    if not groups:
        return "dedup: no exact duplicates found.", False
    redundant = [m.key for g in groups for m in choose_redundant(g, keep=keep)]
    if not apply:
        return f"dedup: would remove {len(redundant)} redundant copy(ies) (keep {keep}).", False
    from cloudcleaner.bulk import bulk_delete

    bulk_delete(adapter, BUCKET, redundant)
    return f"dedup: deleted {len(redundant)} redundant duplicate copy(ies).", False


def step_multipart(apply: bool, opts: dict) -> tuple[str, bool]:
    adapter = _adapter()
    older = opts.get("older_than") or None
    uploads = find_incomplete_uploads(adapter, BUCKET, older_than=older)
    if not uploads:
        return "multipart: no incomplete uploads matched.", False
    if not apply:
        return f"multipart: would abort {len(uploads)} incomplete upload(s).", False
    count, reclaimed = abort_uploads(adapter, BUCKET, uploads)
    return f"multipart: aborted {count} upload(s), reclaimed ~{reclaimed} bytes.", False


def step_quarantine(apply: bool, opts: dict) -> tuple[str, bool]:
    adapter = _adapter()
    config = _config()
    engine = RuleEngine(config)
    result = engine.scan(adapter.list_objects(BUCKET))
    if not result.candidates:
        return "quarantine: no candidates matched the rules.", False
    if not apply:
        return f"quarantine: would quarantine {result.candidate_count} object(s).", False
    from cloudcleaner.guardrail import GuardrailLimits, GuardrailViolation, check_guardrail

    if not opts.get("force"):
        try:
            check_guardrail(result, GuardrailLimits.from_dict(config.quarantine.guardrail))
        except GuardrailViolation as exc:
            return f"quarantine: guardrail: {exc}", True
    manager = QuarantineManager(adapter, config)
    manifest = manager.quarantine(result.candidates)
    return f"quarantine: moved {len(manifest.entries)} object(s) as batch {manifest.batch_id}.", False


def step_purge(apply: bool, opts: dict) -> tuple[str, bool]:
    adapter = _adapter()
    config = _config()
    manager = QuarantineManager(adapter, config)
    batches = manager.list_batches() if opts.get("force") else manager.expired_batches()
    if not batches:
        return "purge: no purgeable batches (retention still open; tick force to override).", False
    if not apply:
        n = sum(len(m.entries) for m in batches)
        return f"purge: would permanently delete {n} object(s).", False
    freed = manager.purge(batches)
    return f"purge: permanently deleted {freed} bytes across {len(batches)} batch(es).", False


def step_restore(apply: bool, opts: dict) -> tuple[str, bool]:
    adapter = _adapter()
    config = _config()
    manager = QuarantineManager(adapter, config)
    batches = manager.list_batches()
    if not batches:
        return "restore: no quarantine batches to restore.", False
    if not apply:
        n = sum(len(m.entries) for m in batches)
        return f"restore: would restore {n} object(s) to original keys.", False
    total = sum(len(manager.restore(m.batch_id)) for m in batches)
    return f"restore: returned {total} object(s) to their original keys.", False


STEPS = {
    "dedup": step_dedup,
    "multipart": step_multipart,
    "quarantine": step_quarantine,
    "purge": step_purge,
    "restore": step_restore,
}
# Safe execution order regardless of tick order: reclaim/quarantine first,
# purge last, restore only when explicitly chosen (mutually exclusive-ish).
RUN_ORDER = ["dedup", "multipart", "quarantine", "purge", "restore"]


def action_run(payload: dict) -> dict:
    """Run a composed cleanup: the ticked steps, each with its own options,
    under one master apply/force switch (per-step force may also be set)."""
    apply = bool(payload.get("apply", False))
    steps = payload.get("steps", {}) or {}
    selected = [name for name in RUN_ORDER if steps.get(name, {}).get("enabled")]
    if not selected:
        return {"error": "No cleans selected — tick at least one.", "state": get_state()}
    messages, had_error = [], False
    for name in selected:
        opts = dict(steps.get(name, {}))
        try:
            msg, is_err = STEPS[name](apply, opts)
        except Exception as exc:  # noqa: BLE001
            msg, is_err = f"{name}: error: {exc}", True
        messages.append(msg)
        had_error = had_error or is_err
    header = ("Applied" if apply else "Dry run —") + f" {len(selected)} clean(s):"
    return {
        "message": header,
        "steps_result": messages,
        "error": ("one or more steps reported a problem" if had_error else None),
        "state": get_state(),
    }


ACTIONS = {
    "scan": lambda p: {"message": "Scan complete.", "state": get_state()},
    "run": action_run,
    "reset": lambda p: {"message": "Bucket reset (small demo scenario).", "state": seed_bucket()},
    "reset_large": lambda p: {
        "message": "Bucket seeded with multi-GB data (real storage — reset when done).",
        "state": seed_large(),
    },
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
