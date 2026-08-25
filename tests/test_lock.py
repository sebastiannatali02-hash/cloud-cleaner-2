from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.lock import DistributedLock, LockError

BUCKET = "b"
T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def lock(adapter, owner, now=T0, ttl=3600):
    return DistributedLock(adapter, BUCKET, owner=owner, ttl_seconds=ttl, now=now)


def test_acquire_is_mutually_exclusive():
    a = MemoryAdapter()
    first = lock(a, "A")
    second = lock(a, "B")
    assert first.acquire() is True
    assert second.acquire() is False  # A holds it, not expired


def test_release_frees_the_lock():
    a = MemoryAdapter()
    first = lock(a, "A")
    assert first.acquire() is True
    assert first.release() is True
    # now B can take it
    assert lock(a, "B").acquire() is True


def test_expired_lock_can_be_taken_over():
    a = MemoryAdapter()
    holder = lock(a, "A", now=T0, ttl=60)
    assert holder.acquire() is True
    # 61s later the TTL has passed; B may steal it
    later = lock(a, "B", now=T0 + timedelta(seconds=61), ttl=60)
    assert later.acquire() is True


def test_live_lock_cannot_be_taken_over():
    a = MemoryAdapter()
    assert lock(a, "A", now=T0, ttl=300).acquire() is True
    # 60s later, still within the 300s TTL
    assert lock(a, "B", now=T0 + timedelta(seconds=60), ttl=300).acquire() is False


def test_release_does_not_delete_a_taken_over_lock():
    a = MemoryAdapter()
    stale = lock(a, "A", now=T0, ttl=60)
    assert stale.acquire() is True
    # B takes over after expiry
    fresh = lock(a, "B", now=T0 + timedelta(seconds=61), ttl=60)
    assert fresh.acquire() is True
    # A (the stale holder) tries to release — must NOT remove B's lock
    assert stale.release() is False
    # B still holds it: a new acquirer is refused
    assert lock(a, "C", now=T0 + timedelta(seconds=62), ttl=60).acquire() is False


def test_context_manager_raises_when_held():
    a = MemoryAdapter()
    outer = lock(a, "A")
    assert outer.acquire() is True
    with pytest.raises(LockError):
        with lock(a, "B"):
            pass


def test_context_manager_releases_on_exit():
    a = MemoryAdapter()
    with lock(a, "A"):
        assert lock(a, "B").acquire() is False  # locked inside the block
    # released on exit
    assert lock(a, "B").acquire() is True


def test_put_if_absent_is_atomic_on_memory_adapter():
    a = MemoryAdapter()
    assert a.put_if_absent(BUCKET, "k", "first") is True
    assert a.put_if_absent(BUCKET, "k", "second") is False
    assert a.get_text(BUCKET, "k") == "first"
