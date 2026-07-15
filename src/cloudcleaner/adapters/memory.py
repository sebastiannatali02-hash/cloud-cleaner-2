"""In-memory adapter used by the test suite and the `demo` command."""

from __future__ import annotations

from typing import Iterator

from ..models import StorageObject


class MemoryAdapter:
    def __init__(self):
        # bucket -> key -> (StorageObject, text content or None)
        self.buckets: dict[str, dict[str, tuple[StorageObject, str | None]]] = {}

    def seed(self, bucket: str, objects: list[StorageObject]) -> None:
        store = self.buckets.setdefault(bucket, {})
        for obj in objects:
            store[obj.key] = (obj, None)

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[StorageObject]:
        for key in sorted(self.buckets.get(bucket, {})):
            if key.startswith(prefix):
                yield self.buckets[bucket][key][0]

    def copy(self, bucket: str, key: str, dst_bucket: str, dst_key: str) -> None:
        obj, text = self.buckets[bucket][key]
        new_obj = StorageObject(
            key=dst_key,
            size_bytes=obj.size_bytes,
            last_modified=obj.last_modified,
            storage_class=obj.storage_class,
        )
        self.buckets.setdefault(dst_bucket, {})[dst_key] = (new_obj, text)

    def delete(self, bucket: str, key: str) -> None:
        self.buckets.get(bucket, {}).pop(key, None)

    def put_text(self, bucket: str, key: str, text: str) -> None:
        from datetime import datetime, timezone

        obj = StorageObject(
            key=key,
            size_bytes=len(text.encode("utf-8")),
            last_modified=datetime.now(timezone.utc),
        )
        self.buckets.setdefault(bucket, {})[key] = (obj, text)

    def get_text(self, bucket: str, key: str) -> str:
        text = self.buckets[bucket][key][1]
        if text is None:
            raise KeyError(f"{key} has no text content")
        return text
