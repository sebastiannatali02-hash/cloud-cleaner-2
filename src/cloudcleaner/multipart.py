"""Detect and clean up incomplete multipart uploads.

Failed or abandoned multipart uploads leave orphaned parts that the
cloud provider bills for but that never appear in a normal object
listing. This module is provider-agnostic: it only speaks to the
``StorageAdapter`` interface (``list_multipart_uploads`` /
``abort_multipart_upload``), so it works against S3, the in-memory
adapter, or any future backend that grows those methods.

The split of responsibilities mirrors the rest of the tool: this module
*finds* incomplete uploads and, separately, *aborts* a given set of
them. Whether a run is a dry-run (find only) or an apply (find + abort)
is decided by the caller (the CLI).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from cloudcleaner.config import parse_cutoff


@dataclass(frozen=True)
class IncompleteUpload:
    """A single in-progress/incomplete multipart upload."""

    key: str
    upload_id: str
    initiated: datetime | None
    size_bytes: int | None = None


def find_incomplete_uploads(
    adapter,
    bucket: str,
    older_than: str | datetime | None = None,
    now: datetime | None = None,
) -> list[IncompleteUpload]:
    """Return the incomplete multipart uploads in ``bucket``.

    ``older_than`` (optional) filters to uploads *initiated* at or before
    an age cutoff, using the same period grammar as the config's
    ``older_than`` ("90d", "8w", "6m", "10y") or an ISO date. Uploads
    with an unknown initiation time are excluded when a cutoff is given
    (we cannot prove they are old enough).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = parse_cutoff(older_than, now) if older_than is not None else None

    results: list[IncompleteUpload] = []
    for record in adapter.list_multipart_uploads(bucket):
        initiated = record.get("initiated")
        if cutoff is not None:
            if initiated is None:
                continue
            if _as_aware(initiated) > cutoff:
                continue
        results.append(
            IncompleteUpload(
                key=record["key"],
                upload_id=record["upload_id"],
                initiated=initiated,
                size_bytes=record.get("size_bytes"),
            )
        )
    return results


def abort_uploads(adapter, bucket: str, uploads) -> tuple[int, int]:
    """Abort each upload in ``uploads``.

    Returns ``(count_aborted, bytes_reclaimed)`` where ``bytes_reclaimed``
    sums the known aggregate part sizes (uploads with an unknown size
    contribute 0). The counter reflects how many aborts were issued.
    """
    count = 0
    reclaimed = 0
    for upload in uploads:
        adapter.abort_multipart_upload(bucket, upload.key, upload.upload_id)
        count += 1
        if upload.size_bytes:
            reclaimed += upload.size_bytes
    return count, reclaimed


def _as_aware(value: datetime) -> datetime:
    """Treat naive datetimes as UTC so comparisons never explode."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
