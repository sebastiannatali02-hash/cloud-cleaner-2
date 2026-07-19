"""Google Cloud Storage adapter.

Credentials come from the standard google-auth chain (the
``GOOGLE_APPLICATION_CREDENTIALS`` env var, gcloud application-default
credentials, or the attached service account on GCE/GKE).

The ``google-cloud-storage`` SDK is imported lazily (inside
:meth:`__init__`) so it is only required when the ``gcs`` provider is
actually used. Tests inject a pre-built ``client`` to exercise the
adapter without the SDK installed.
"""

from __future__ import annotations

from typing import Iterator

from cloudcleaner.models import StorageObject


class GCSAdapter:
    def __init__(self, project: str | None = None, client=None):
        if client is None:
            from google.cloud import storage  # lazy import

            client = storage.Client(project=project)
        self._client = client

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[StorageObject]:
        for blob in self._client.list_blobs(bucket, prefix=prefix):
            yield StorageObject(
                key=blob.name,
                size_bytes=blob.size or 0,
                last_modified=blob.updated,
                storage_class=blob.storage_class or "STANDARD",
            )

    def copy(self, bucket: str, key: str, dst_bucket: str, dst_key: str) -> None:
        src_bucket = self._client.bucket(bucket)
        src_blob = src_bucket.blob(key)
        dst = self._client.bucket(dst_bucket)
        src_bucket.copy_blob(src_blob, dst, dst_key)

    def delete(self, bucket: str, key: str) -> None:
        self._client.bucket(bucket).blob(key).delete()

    def put_text(self, bucket: str, key: str, text: str) -> None:
        blob = self._client.bucket(bucket).blob(key)
        blob.upload_from_string(text, content_type="text/plain")

    def get_text(self, bucket: str, key: str) -> str:
        blob = self._client.bucket(bucket).blob(key)
        return blob.download_as_bytes().decode("utf-8")
