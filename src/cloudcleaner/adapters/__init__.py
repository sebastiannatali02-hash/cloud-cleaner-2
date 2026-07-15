"""Storage adapters.

The rest of the tool only speaks :class:`StorageAdapter`, so adding a
new provider (Azure Blob, GCS, ...) means implementing this interface
and registering it in :func:`get_adapter`.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..models import StorageObject


class StorageAdapter(Protocol):
    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[StorageObject]:
        """Yield every object in the bucket under the given prefix."""
        ...

    def copy(self, bucket: str, key: str, dst_bucket: str, dst_key: str) -> None:
        ...

    def delete(self, bucket: str, key: str) -> None:
        ...

    def put_text(self, bucket: str, key: str, text: str) -> None:
        ...

    def get_text(self, bucket: str, key: str) -> str:
        ...


def get_adapter(config) -> StorageAdapter:
    if config.provider == "s3":
        from .s3 import S3Adapter

        return S3Adapter(region=config.region, endpoint_url=config.endpoint_url)
    if config.provider == "memory":
        from .memory import MemoryAdapter

        return MemoryAdapter()
    raise ValueError(f"unknown provider {config.provider!r} (expected 's3' or 'memory')")
