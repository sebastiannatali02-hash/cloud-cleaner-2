"""Cross-process / cross-host advisory lock backed by the storage bucket.

cloudcleaner mutating operations (quarantine, purge, restore) assume they are
the only writer against a bucket — concurrent runs interleave manifest writes
and corrupt batches. A single in-process lock only coordinates one process; a
second host, cron job, or CLI invocation would still race.

This lock coordinates through the bucket itself, using the adapter's atomic
``put_if_absent`` (S3 conditional write ``If-None-Match: *``). Exactly one
caller can create the lock object, so exactly one holds the lock — across
processes and hosts.

Design notes:
  * TTL — the lock object records an expiry. A holder that crashes without
    releasing does not deadlock the bucket forever; once the TTL passes, any
    caller may take over the (expired) lock. Long operations should pass a TTL
    comfortably longer than their worst-case runtime.
  * Fencing token — each acquisition writes a unique token. ``release`` only
    deletes the lock if the stored token still matches, so a slow holder whose
    lock already expired and was taken over cannot delete the new holder's
    lock.
  * Advisory — like every lock, it only works if all writers use it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_LOCK_KEY = "_cloudcleaner/LOCK"
DEFAULT_TTL_SECONDS = 3600  # 1h — longer than any expected single operation


class LockError(Exception):
    """Raised when the lock cannot be acquired (already held and not expired)."""


@dataclass
class LockInfo:
    token: str
    owner: str
    acquired_at: str
    expires_at: str

    def is_expired(self, now: datetime) -> bool:
        return datetime.fromisoformat(self.expires_at) <= now

    def to_json(self) -> str:
        return json.dumps(vars(self))

    @classmethod
    def from_json(cls, text: str) -> "LockInfo":
        return cls(**json.loads(text))


class DistributedLock:
    """A TTL'd, token-fenced advisory lock stored in the bucket.

    Usage::

        lock = DistributedLock(adapter, bucket, owner="host-A/pid-123")
        if not lock.acquire():
            ... # someone else holds it
        try:
            ...  # do the exclusive work
        finally:
            lock.release()

    or as a context manager (raises LockError if it cannot acquire)::

        with DistributedLock(adapter, bucket, owner=...):
            ...
    """

    def __init__(
        self,
        adapter,
        bucket: str,
        *,
        owner: str,
        key: str = DEFAULT_LOCK_KEY,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        token: str | None = None,
        now: datetime | None = None,
    ):
        self.adapter = adapter
        self.bucket = bucket
        self.owner = owner
        self.key = key
        self.ttl = timedelta(seconds=ttl_seconds)
        # Token uniquely identifies THIS acquisition. Deterministic input is
        # allowed for tests; otherwise derived from owner + acquire time.
        self._token = token
        self._now = now
        self._held = False

    def _clock(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def _read(self) -> LockInfo | None:
        try:
            return LockInfo.from_json(self.adapter.get_text(self.bucket, self.key))
        except KeyError:
            return None

    def current_holder(self) -> LockInfo | None:
        """The lock's current holder (or None if free) — for error messages."""
        return self._read()

    def acquire(self) -> bool:
        """Try to take the lock. Returns True on success, False if another
        live (non-expired) holder has it. Takes over an expired lock."""
        now = self._clock()
        token = self._token or f"{self.owner}:{now.isoformat()}"
        info = LockInfo(
            token=token,
            owner=self.owner,
            acquired_at=now.isoformat(),
            expires_at=(now + self.ttl).isoformat(),
        )

        # Fast path: atomic create-if-absent wins the lock outright.
        if self.adapter.put_if_absent(self.bucket, self.key, info.to_json()):
            self._token, self._held = token, True
            return True

        # Object exists — take over only if the current holder's TTL expired.
        current = self._read()
        if current is not None and not current.is_expired(now):
            return False

        # Expired (or unreadable/stale): steal it. Delete then re-create
        # atomically so a racing acquirer still contends on put_if_absent.
        self.adapter.delete(self.bucket, self.key)
        if self.adapter.put_if_absent(self.bucket, self.key, info.to_json()):
            self._token, self._held = token, True
            return True
        return False

    def release(self) -> bool:
        """Release the lock iff we still own it (token matches). Returns True
        if we deleted our lock, False if it was already gone or taken over."""
        if not self._held:
            return False
        current = self._read()
        if current is not None and current.token == self._token:
            self.adapter.delete(self.bucket, self.key)
            self._held = False
            return True
        # Our lock expired and someone else took over — do not delete theirs.
        self._held = False
        return False

    def __enter__(self) -> "DistributedLock":
        if not self.acquire():
            holder = self._read()
            who = holder.owner if holder else "another process"
            raise LockError(f"bucket {self.bucket!r} is locked by {who}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
