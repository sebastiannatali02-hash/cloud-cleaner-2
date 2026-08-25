"""Azure Blob Storage adapter.

An Azure "container" plays the role of a bucket and a "blob" the role of
an object. Credentials come from ``DefaultAzureCredential`` (env vars,
managed identity, az-cli login, ...) unless a connection string or a
pre-built client is supplied.

The ``azure-storage-blob`` SDK is imported lazily (inside
:meth:`__init__`) so it is only required when the ``azure`` provider is
actually used. Tests inject a pre-built ``client`` (a
``BlobServiceClient``) to exercise the adapter without the SDK
installed.
"""

from __future__ import annotations

from typing import Iterator

from cloudcleaner.models import StorageObject


class AzureAdapter:
    def __init__(
        self,
        account_url: str | None = None,
        connection_string: str | None = None,
        client=None,
    ):
        if client is None:
            from azure.storage.blob import BlobServiceClient  # lazy import

            if connection_string:
                client = BlobServiceClient.from_connection_string(connection_string)
            else:
                from azure.identity import DefaultAzureCredential  # lazy import

                client = BlobServiceClient(
                    account_url=account_url, credential=DefaultAzureCredential()
                )
        self._client = client

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[StorageObject]:
        container = self._client.get_container_client(bucket)
        for blob in container.list_blobs(name_starts_with=prefix):
            yield StorageObject(
                key=blob.name,
                size_bytes=blob.size or 0,
                last_modified=blob.last_modified,
                storage_class=(blob.blob_tier or "STANDARD"),
            )

    def copy(self, bucket: str, key: str, dst_bucket: str, dst_key: str) -> None:
        source = self._client.get_blob_client(container=bucket, blob=key)
        dst = self._client.get_blob_client(container=dst_bucket, blob=dst_key)
        dst.start_copy_from_url(source.url)

    def delete(self, bucket: str, key: str) -> None:
        self._client.get_blob_client(container=bucket, blob=key).delete_blob()

    def put_text(self, bucket: str, key: str, text: str) -> None:
        blob = self._client.get_blob_client(container=bucket, blob=key)
        blob.upload_blob(text.encode("utf-8"), overwrite=True)

    def get_text(self, bucket: str, key: str) -> str:
        blob = self._client.get_blob_client(container=bucket, blob=key)
        return blob.download_blob().readall().decode("utf-8")
