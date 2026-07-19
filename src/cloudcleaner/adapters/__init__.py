"""Storage adapters.

The rest of the tool only speaks :class:`StorageAdapter` (dependency
inversion): no module outside this package knows about boto3 or any
concrete backend. New providers (Azure Blob, GCS, ...) plug in through
:func:`register_adapter` without touching existing code (open/closed):

    @register_adapter("azure")
    def _azure_factory(config: Config) -> StorageAdapter:
        from cloudcleaner.adapters.azure import AzureAdapter
        return AzureAdapter(...)

Built-in factories import their backend lazily so that e.g. boto3 is
only loaded when the s3 provider is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterator, Protocol

from cloudcleaner.models import StorageObject

if TYPE_CHECKING:
    from cloudcleaner.config import Config


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


AdapterFactory = Callable[["Config"], StorageAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str) -> Callable[[AdapterFactory], AdapterFactory]:
    """Register a factory for a provider name; use as a decorator."""

    def decorator(factory: AdapterFactory) -> AdapterFactory:
        _REGISTRY[name] = factory
        return factory

    return decorator


def get_adapter(config: "Config") -> StorageAdapter:
    try:
        factory = _REGISTRY[config.provider]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"unknown provider {config.provider!r} (available: {known})") from None
    return factory(config)


@register_adapter("s3")
def _s3_factory(config: "Config") -> StorageAdapter:
    from cloudcleaner.adapters.s3 import S3Adapter

    return S3Adapter(region=config.region, endpoint_url=config.endpoint_url)


@register_adapter("memory")
def _memory_factory(config: "Config") -> StorageAdapter:
    from cloudcleaner.adapters.memory import MemoryAdapter

    return MemoryAdapter()
