"""Bulk-delete helper.

Deleting a large purge batch one object at a time is slow and, on S3,
one API call per key. Adapters may optionally expose ``delete_many`` to
delete in a single (batched) request; :func:`bulk_delete` uses it when
present and otherwise falls back to per-key :meth:`delete`, so adapters
that only implement the required ``delete`` still work unchanged.
"""

from __future__ import annotations

from typing import Iterable

from cloudcleaner.adapters import StorageAdapter


def bulk_delete(adapter: StorageAdapter, bucket: str, keys: Iterable[str]) -> int:
    """Delete every key from the bucket; return how many were deleted.

    Uses ``adapter.delete_many`` when the adapter provides it (S3's
    batched ``delete_objects``), else calls ``adapter.delete`` per key.
    """
    keys = list(keys)
    delete_many = getattr(adapter, "delete_many", None)
    if callable(delete_many):
        delete_many(bucket, keys)
    else:
        for key in keys:
            adapter.delete(bucket, key)
    return len(keys)
