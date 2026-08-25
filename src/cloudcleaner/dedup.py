"""Exact-duplicate object detection from listing metadata alone.

S3 exposes an ``ETag`` per object. For objects uploaded in a single
(non-multipart) request, the ETag is the MD5 of the content, so
byte-identical objects share an ETag and can be found without ever
downloading them.

Multipart-uploaded objects have an ETag of the form ``"<md5>-<partcount>"``
(note the ``-``); that value is *not* a plain content MD5 and cannot be
compared across objects reliably, so such objects are skipped entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from cloudcleaner.models import StorageObject


def _normalize_etag(etag: str | None) -> str | None:
    """Strip the surrounding quotes S3 wraps ETags in.

    Returns ``None`` for a missing etag or a multipart etag (one that
    contains a ``-``), both of which are unusable as a content hash.
    """
    if not etag:
        return None
    normalized = etag.strip().strip('"')
    if not normalized or "-" in normalized:
        return None
    return normalized


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of objects that share the same content (etag + size)."""

    etag: str
    size_bytes: int
    members: tuple[StorageObject, ...]

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def redundant_bytes(self) -> int:
        """Bytes reclaimable if all but one copy is removed."""
        return self.size_bytes * (self.count - 1)


def find_duplicates(
    objects: Iterable[StorageObject], min_size: int = 0
) -> list[DuplicateGroup]:
    """Group objects by (normalized etag, size) and return 2+ member groups.

    Objects with no etag or a multipart etag (contains ``-``) are skipped,
    as are objects smaller than ``min_size``.
    """
    groups: dict[tuple[str, int], list[StorageObject]] = {}
    for obj in objects:
        if obj.size_bytes < min_size:
            continue
        etag = _normalize_etag(obj.etag)
        if etag is None:
            continue
        groups.setdefault((etag, obj.size_bytes), []).append(obj)

    return [
        DuplicateGroup(etag=etag, size_bytes=size, members=tuple(members))
        for (etag, size), members in groups.items()
        if len(members) >= 2
    ]


def choose_redundant(
    group: DuplicateGroup, keep: str = "oldest"
) -> list[StorageObject]:
    """Return members to remove, keeping one canonical copy.

    ``keep='oldest'`` (default) retains the earliest ``last_modified``;
    ``keep='newest'`` retains the latest.
    """
    if keep not in ("oldest", "newest"):
        raise ValueError(f"keep must be 'oldest' or 'newest', got {keep!r}")

    ordered = sorted(group.members, key=lambda o: o.last_modified)
    kept = ordered[0] if keep == "oldest" else ordered[-1]
    return [o for o in group.members if o is not kept]


def total_reclaimable(groups: Iterable[DuplicateGroup]) -> int:
    """Sum of reclaimable bytes across all duplicate groups."""
    return sum(g.redundant_bytes for g in groups)
