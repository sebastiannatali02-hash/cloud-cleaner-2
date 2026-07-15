"""Quarantine lifecycle.

Nothing is ever deleted directly: candidates are first *moved* under a
quarantine prefix together with a JSON manifest. Only after the
retention window expires can `purge` remove them for good, and until
then `restore` can put any of them back exactly where they were.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .adapters import StorageAdapter
from .config import Config
from .models import Candidate

MANIFEST_NAME = "manifest.json"


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

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

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
                "entries": [vars(e) for e in self.entries],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        entries = [ManifestEntry(**e) for e in data.pop("entries", [])]
        return cls(entries=entries, **data)


class QuarantineManager:
    def __init__(self, adapter: StorageAdapter, config: Config, now: datetime | None = None):
        self.adapter = adapter
        self.config = config
        self.now = now or datetime.now(timezone.utc)
        self.prefix = config.quarantine.prefix
        self.q_bucket = config.quarantine.bucket or config.bucket

    def _manifest_key(self, batch_id: str) -> str:
        return f"{self.prefix}{batch_id}/{MANIFEST_NAME}"

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
        for cand in candidates:
            q_key = f"{self.prefix}{batch_id}/objects/{cand.obj.key}"
            self.adapter.copy(self.config.bucket, cand.obj.key, self.q_bucket, q_key)
            self.adapter.delete(self.config.bucket, cand.obj.key)
            manifest.entries.append(
                ManifestEntry(
                    original_key=cand.obj.key,
                    quarantine_key=q_key,
                    size_bytes=cand.obj.size_bytes,
                    rule=cand.rule_name,
                )
            )
        self.adapter.put_text(self.q_bucket, self._manifest_key(batch_id), manifest.to_json())
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
        """Permanently delete the given quarantined batches. Returns bytes freed."""
        freed = 0
        for manifest in batches:
            for entry in manifest.entries:
                self.adapter.delete(self.q_bucket, entry.quarantine_key)
                freed += entry.size_bytes
            self.adapter.delete(self.q_bucket, self._manifest_key(manifest.batch_id))
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
        restored, kept = [], []
        for entry in manifest.entries:
            if wanted is not None and entry.original_key not in wanted:
                kept.append(entry)
                continue
            self.adapter.copy(self.q_bucket, entry.quarantine_key, manifest.bucket, entry.original_key)
            self.adapter.delete(self.q_bucket, entry.quarantine_key)
            restored.append(entry)

        if wanted is not None:
            missing = wanted - {e.original_key for e in restored}
            if missing:
                raise KeyError(f"keys not found in batch {batch_id!r}: {sorted(missing)}")

        if kept:
            manifest.entries = kept
            self.adapter.put_text(self.q_bucket, self._manifest_key(batch_id), manifest.to_json())
        else:
            self.adapter.delete(self.q_bucket, self._manifest_key(batch_id))
        return restored
