"""Tests for incomplete multipart upload detection and cleanup.

The MemoryAdapter tests exercise the provider-agnostic logic; the
S3Adapter tests inject a fake boto3 client (no real AWS) to verify
pagination, field mapping and the abort call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.multipart import (
    IncompleteUpload,
    abort_uploads,
    find_incomplete_uploads,
)

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def _seeded_memory() -> MemoryAdapter:
    adapter = MemoryAdapter()
    adapter.seed_multipart("b", "recent.bin", "u-recent", NOW - timedelta(days=1), 100)
    adapter.seed_multipart("b", "old.bin", "u-old", NOW - timedelta(days=100), 2048)
    adapter.seed_multipart("b", "ancient.bin", "u-ancient", NOW - timedelta(days=400), 4096)
    return adapter


# --------------------------------------------------------------------------- #
# MemoryAdapter / provider-agnostic logic
# --------------------------------------------------------------------------- #
def test_find_returns_all_when_no_cutoff():
    adapter = _seeded_memory()
    found = find_incomplete_uploads(adapter, "b", now=NOW)
    assert {u.upload_id for u in found} == {"u-recent", "u-old", "u-ancient"}
    assert all(isinstance(u, IncompleteUpload) for u in found)


def test_find_filters_by_older_than():
    adapter = _seeded_memory()
    found = find_incomplete_uploads(adapter, "b", older_than="90d", now=NOW)
    assert {u.upload_id for u in found} == {"u-old", "u-ancient"}


def test_find_older_than_iso_date():
    adapter = _seeded_memory()
    found = find_incomplete_uploads(adapter, "b", older_than="2026-01-01", now=NOW)
    # only the 400-day-old upload predates 2026-01-01
    assert {u.upload_id for u in found} == {"u-ancient"}


def test_find_excludes_unknown_initiated_under_cutoff():
    adapter = MemoryAdapter()
    adapter.seed_multipart("b", "no-date.bin", "u-nd", None, 10)
    assert find_incomplete_uploads(adapter, "b", older_than="1d", now=NOW) == []
    # but no cutoff keeps it
    assert len(find_incomplete_uploads(adapter, "b", now=NOW)) == 1


def test_find_empty_bucket():
    assert find_incomplete_uploads(MemoryAdapter(), "nope", now=NOW) == []


def test_abort_uploads_removes_and_counts_bytes():
    adapter = _seeded_memory()
    targets = find_incomplete_uploads(adapter, "b", older_than="90d", now=NOW)
    count, reclaimed = abort_uploads(adapter, "b", targets)
    assert count == 2
    assert reclaimed == 2048 + 4096
    # the aborted uploads are gone; the recent one remains
    remaining = {u.upload_id for u in find_incomplete_uploads(adapter, "b", now=NOW)}
    assert remaining == {"u-recent"}


def test_abort_handles_unknown_size():
    adapter = MemoryAdapter()
    adapter.seed_multipart("b", "k", "u", NOW, None)
    found = find_incomplete_uploads(adapter, "b", now=NOW)
    count, reclaimed = abort_uploads(adapter, "b", found)
    assert count == 1
    assert reclaimed == 0


# --------------------------------------------------------------------------- #
# S3Adapter with a fake boto3 client (no real AWS)
# --------------------------------------------------------------------------- #
class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        for page in self._pages:
            yield page


class FakeS3Client:
    """Mimics the slice of the boto3 S3 client the adapter touches."""

    def __init__(self):
        self.aborted: list[dict] = []
        # list_multipart_uploads paginated across two pages
        self._mpu_pages = [
            {
                "Uploads": [
                    {"Key": "a.bin", "UploadId": "id-a", "Initiated": NOW - timedelta(days=2)},
                ]
            },
            {
                "Uploads": [
                    {"Key": "b.bin", "UploadId": "id-b", "Initiated": NOW - timedelta(days=50)},
                ]
            },
        ]
        # list_parts responses keyed by (key, upload_id)
        self._parts = {
            ("a.bin", "id-a"): [{"Parts": [{"Size": 10}, {"Size": 5}]}],
            ("b.bin", "id-b"): [{"Parts": [{"Size": 100}]}],
        }

    def get_paginator(self, name):
        if name == "list_multipart_uploads":
            return FakePaginator(self._mpu_pages)
        if name == "list_parts":
            self._pending_parts_name = name
            return self._PartsPaginator(self)
        raise AssertionError(f"unexpected paginator {name}")

    class _PartsPaginator:
        def __init__(self, client):
            self._client = client

        def paginate(self, *, Bucket, Key, UploadId):
            return iter(self._client._parts.get((Key, UploadId), []))

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.aborted.append({"Bucket": Bucket, "Key": Key, "UploadId": UploadId})


@pytest.fixture
def s3_adapter(monkeypatch):
    import cloudcleaner.adapters.s3 as s3mod

    fake = FakeS3Client()
    # Prevent the real boto3.client from ever being called.
    monkeypatch.setattr(s3mod.boto3, "client", lambda *a, **k: fake)
    adapter = s3mod.S3Adapter(region="us-east-1")
    return adapter, fake


def test_s3_list_multipart_paginates_and_maps(s3_adapter):
    adapter, _ = s3_adapter
    records = list(adapter.list_multipart_uploads("bucket"))
    assert [r["key"] for r in records] == ["a.bin", "b.bin"]
    assert [r["upload_id"] for r in records] == ["id-a", "id-b"]
    assert records[0]["initiated"] == NOW - timedelta(days=2)
    # aggregate size summed via list_parts
    assert records[0]["size_bytes"] == 15
    assert records[1]["size_bytes"] == 100


def test_s3_list_multipart_feeds_find(s3_adapter):
    adapter, _ = s3_adapter
    found = find_incomplete_uploads(adapter, "bucket", older_than="30d", now=NOW)
    assert {u.upload_id for u in found} == {"id-b"}


def test_s3_abort_calls_client(s3_adapter):
    adapter, fake = s3_adapter
    adapter.abort_multipart_upload("bucket", "a.bin", "id-a")
    assert fake.aborted == [{"Bucket": "bucket", "Key": "a.bin", "UploadId": "id-a"}]


def test_s3_size_degrades_to_none_on_error(monkeypatch):
    import cloudcleaner.adapters.s3 as s3mod

    class BrokenPartsClient(FakeS3Client):
        def get_paginator(self, name):
            if name == "list_parts":
                raise RuntimeError("boom")
            return super().get_paginator(name)

    fake = BrokenPartsClient()
    monkeypatch.setattr(s3mod.boto3, "client", lambda *a, **k: fake)
    adapter = s3mod.S3Adapter()
    records = list(adapter.list_multipart_uploads("bucket"))
    assert all(r["size_bytes"] is None for r in records)
