"""Quarantine lifecycle.

Nothing is ever deleted directly: candidates are first *moved* under a
quarantine prefix together with a JSON manifest. Only after the
retention window expires can `purge` remove them for good, and until
then `restore` can put any of them back exactly where they were.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from datetime import datetime, timedelta, timezone

from cloudcleaner.adapters import StorageAdapter
from cloudcleaner.config import Config
from cloudcleaner.models import Candidate

MANIFEST_NAME = "manifest.json"
AUDIT_NAME = "audit.log"

# S3 copy/delete are network-bound, so a moderate thread pool gives a
# near-linear speedup when quarantining thousands of objects. Kept modest
# to stay well under S3 request-rate limits per prefix.
DEFAULT_MAX_WORKERS = 16


class ManifestIntegrityError(Exception):
    """Raised when a manifest's stored checksum does not match its entries.

    A mismatch means the manifest was tampered with or corrupted, so the
    list of quarantined objects can no longer be trusted. Callers must
    refuse to purge or restore such a batch.
    """


def _entries_digest(entries: list["ManifestEntry"]) -> str:
    """SHA-256 over a canonical serialization of the manifest entries.

    Entry order is preserved (it mirrors the objects as quarantined) while
    the keys within each entry are sorted and whitespace is stripped so the
    digest is stable regardless of how the JSON happens to be formatted.
    """
    canonical = json.dumps(
        [vars(e) for e in entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ManifestEntry:
    original_key: str
    quarantine_key: str
    size_bytes: int
    rule: str


@dataclass
class Manifest:
    batch_id: str
    bucket: str
    quarantine_bucket: str
    created_at: str
    purge_after: str
    entries: list[ManifestEntry] = field(default_factory=list)
    # True once ``from_json`` has confirmed the stored checksum, or for a
    # freshly built manifest. False only for a legacy manifest that carried
    # no checksum field (accepted for backward compatibility but unverified).
    integrity_verified: bool = True

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    @property
    def checksum(self) -> str:
        return _entries_digest(self.entries)

    def is_expired(self, now: datetime) -> bool:
        return datetime.fromisoformat(self.purge_after) <= now

    def to_json(self) -> str:
        return json.dumps(
            {
                "batch_id": self.batch_id,
                "bucket": self.bucket,
                "quarantine_bucket": self.quarantine_bucket,
                "created_at": self.created_at,
                "purge_after": self.purge_after,
                "checksum": self.checksum,
                "entries": [vars(e) for e in self.entries],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        entries = [ManifestEntry(**e) for e in data.pop("entries", [])]
        stored = data.pop("checksum", None)
        if stored is not None:
            actual = _entries_digest(entries)
            if stored != actual:
                batch = data.get("batch_id", "<unknown>")
                raise ManifestIntegrityError(
                    f"manifest for batch {batch!r} failed integrity check: "
                    f"stored checksum {stored} != computed {actual}. "
                    "The manifest may have been tampered with or corrupted; "
                    "refusing to trust its contents."
                )
        return cls(entries=entries, integrity_verified=stored is not None, **data)


class QuarantineManager:
    def __init__(
        self,
        adapter: StorageAdapter,
        config: Config,
        now: datetime | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        progress=None,
    ):
        self.adapter = adapter
        self.config = config
        self.now = now or datetime.now(timezone.utc)
        self.prefix = config.quarantine.prefix
        self.q_bucket = config.quarantine.bucket or config.bucket
        self.max_workers = max(1, max_workers)
        # Optional progress sink: called as progress(done, total, action) after
        # each object is processed. Invoked under a lock so it is thread-safe
        # even when work runs across the pool. Defaults to a no-op.
        self._progress = progress or (lambda done, total, action: None)
        self._progress_lock = Lock()

    def _tick(self, total: int, action: str) -> int:
        """Atomically advance the progress counter and notify the sink.
        Returns the new count. Safe to call from pool threads."""
        with self._progress_lock:
            self._done += 1
            done = self._done
            self._progress(done, total, action)
        return done

    def _manifest_key(self, batch_id: str) -> str:
        return f"{self.prefix}{batch_id}/{MANIFEST_NAME}"

    def _audit_key(self) -> str:
        """Key of the append-only audit log.

        The log records every quarantine/restore/purge and lives *inside*
        the quarantine prefix. That placement is deliberate on two counts:

        * ``purge`` only ever deletes the specific per-batch object and
          manifest keys it is given — it never blanket-wipes the prefix —
          so the audit trail survives every purge.
        * the rule engine always excludes the quarantine prefix, so the
          audit log can never itself be matched as a cleanup candidate
          (an audit log named ``audit.log`` would otherwise be caught by a
          ``keyword: log`` rule and quarantined/deleted).
        """
        return f"{self.prefix}{AUDIT_NAME}"

    def _append_audit(
        self, action: str, batch_id: str, entries: list[ManifestEntry]
    ) -> None:
        record = {
            "timestamp": self.now.isoformat(),
            "action": action,
            "batch_id": batch_id,
            "object_count": len(entries),
            "total_bytes": sum(e.size_bytes for e in entries),
            "keys": [e.original_key for e in entries],
        }
        line = json.dumps(record, sort_keys=True)
        try:
            existing = self.adapter.get_text(self.q_bucket, self._audit_key())
        except KeyError:
            existing = ""
        self.adapter.put_text(self.q_bucket, self._audit_key(), existing + line + "\n")

    def read_audit_log(self) -> list[dict]:
        """Return the audit entries, oldest first (empty if none yet)."""
        try:
            text = self.adapter.get_text(self.q_bucket, self._audit_key())
        except KeyError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def quarantine(self, candidates: list[Candidate]) -> Manifest:
        """Move candidates under the quarantine prefix and write the manifest."""
        batch_id = self.now.strftime("%Y%m%dT%H%M%SZ")
        purge_after = self.now + timedelta(days=self.config.quarantine.retention_days)
        manifest = Manifest(
            batch_id=batch_id,
            bucket=self.config.bucket,
            quarantine_bucket=self.q_bucket,
            created_at=self.now.isoformat(),
            purge_after=purge_after.isoformat(),
        )
        total = len(candidates)
        self._done = 0

        def _move(cand: Candidate) -> ManifestEntry:
            q_key = f"{self.prefix}{batch_id}/objects/{cand.obj.key}"
            self.adapter.copy(self.config.bucket, cand.obj.key, self.q_bucket, q_key)
            self.adapter.delete(self.config.bucket, cand.obj.key)
            self._tick(total, "quarantine")
            return ManifestEntry(
                original_key=cand.obj.key,
                quarantine_key=q_key,
                size_bytes=cand.obj.size_bytes,
                rule=cand.rule_name,
            )

        # Copy+delete are network-bound; run them concurrently. executor.map
        # preserves input order, so the manifest entries stay deterministic.
        workers = min(self.max_workers, total) or 1
        if workers == 1:
            manifest.entries = [_move(c) for c in candidates]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                manifest.entries = list(pool.map(_move, candidates))
        self.adapter.put_text(self.q_bucket, self._manifest_key(batch_id), manifest.to_json())
        self._append_audit("quarantine", batch_id, manifest.entries)
        return manifest

    def list_batches(self) -> list[Manifest]:
        manifests = []
        for obj in self.adapter.list_objects(self.q_bucket, prefix=self.prefix):
            if obj.key.endswith("/" + MANIFEST_NAME):
                manifests.append(Manifest.from_json(self.adapter.get_text(self.q_bucket, obj.key)))
        return sorted(manifests, key=lambda m: m.batch_id)

    def expired_batches(self) -> list[Manifest]:
        return [m for m in self.list_batches() if m.is_expired(self.now)]

    def purge(self, batches: list[Manifest]) -> int:
        """Permanently delete the given quarantined batches. Returns bytes freed.

        Deletion goes through :func:`bulk_delete`, which uses the adapter's
        bulk ``delete_many`` (S3 ``delete_objects``, 1000 keys/request) when
        available and falls back to per-key deletes otherwise — the
        difference between one request per 1000 objects and one per object
        when purging at scale.
        """
        from cloudcleaner.bulk import bulk_delete

        freed = 0
        for manifest in batches:
            keys = [entry.quarantine_key for entry in manifest.entries]
            keys.append(self._manifest_key(manifest.batch_id))
            bulk_delete(self.adapter, self.q_bucket, keys)
            freed += manifest.total_bytes
            self._append_audit("purge", manifest.batch_id, manifest.entries)
        return freed

    def restore(self, batch_id: str, keys: list[str] | None = None) -> list[ManifestEntry]:
        """Put quarantined objects back at their original location.

        ``keys`` restricts the restore to specific original keys;
        by default the whole batch is restored.
        """
        batches = {m.batch_id: m for m in self.list_batches()}
        if batch_id not in batches:
            raise KeyError(f"no quarantine batch {batch_id!r}")
        manifest = batches[batch_id]

        wanted = set(keys) if keys else None
        targets = [e for e in manifest.entries if wanted is None or e.original_key in wanted]
        kept = [e for e in manifest.entries if wanted is not None and e.original_key not in wanted]
        total = len(targets)
        self._done = 0

        # List the batch's surviving quarantine objects once, so the crash-safe
        # "already restored?" check is a set lookup rather than one list call
        # per entry (which would double the API traffic on the happy path).
        batch_object_prefix = f"{self.prefix}{manifest.batch_id}/objects/"
        present = {
            o.key for o in self.adapter.list_objects(self.q_bucket, prefix=batch_object_prefix)
        }

        def _restore_one(entry: ManifestEntry) -> ManifestEntry:
            # A missing quarantine copy means a prior restore was interrupted
            # after moving this object back but before rewriting the manifest;
            # treat it as already-restored so a re-run converges (idempotent).
            if entry.quarantine_key in present:
                self.adapter.copy(
                    self.q_bucket, entry.quarantine_key, manifest.bucket, entry.original_key
                )
                self.adapter.delete(self.q_bucket, entry.quarantine_key)
            self._tick(total, "restore")
            return entry

        # Copy+delete per object are network-bound; run them concurrently.
        workers = min(self.max_workers, total) or 1
        if workers == 1:
            restored = [_restore_one(e) for e in targets]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                restored = list(pool.map(_restore_one, targets))

        if wanted is not None:
            missing = wanted - {e.original_key for e in restored}
            if missing:
                raise KeyError(f"keys not found in batch {batch_id!r}: {sorted(missing)}")

        if kept:
            manifest.entries = kept
            self.adapter.put_text(self.q_bucket, self._manifest_key(batch_id), manifest.to_json())
        else:
            self.adapter.delete(self.q_bucket, self._manifest_key(batch_id))
        self._append_audit("restore", batch_id, restored)
        return restored
