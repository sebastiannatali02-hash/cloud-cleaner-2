"""AWS S3 adapter.

Credentials come from the standard boto3 chain (env vars, shared
credentials file, instance profile). ``endpoint_url`` allows pointing
at S3-compatible storage (MinIO, Cloudflare R2, ...).
"""

from __future__ import annotations

from typing import Iterable, Iterator

import boto3

from cloudcleaner.models import StorageObject


class S3Adapter:
    def __init__(self, region: str | None = None, endpoint_url: str | None = None):
        self._client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def list_objects(self, bucket: str, prefix: str = "") -> Iterator[StorageObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield StorageObject(
                    key=item["Key"],
                    size_bytes=item["Size"],
                    last_modified=item["LastModified"],
                    storage_class=item.get("StorageClass", "STANDARD"),
                )

    def copy(self, bucket: str, key: str, dst_bucket: str, dst_key: str) -> None:
        self._client.copy(
            CopySource={"Bucket": bucket, "Key": key}, Bucket=dst_bucket, Key=dst_key
        )

    def delete(self, bucket: str, key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=key)

    def delete_many(self, bucket: str, keys: Iterable[str]) -> None:
        """Bulk-delete objects using the S3 ``delete_objects`` API.

        S3 accepts at most 1000 keys per request, so keys are chunked
        into batches of that size. This is far cheaper (one request per
        1000 keys instead of one per key) when purging at scale.
        """
        batch: list[dict[str, str]] = []
        for key in keys:
            batch.append({"Key": key})
            if len(batch) == 1000:
                self._client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                batch = []
        if batch:
            self._client.delete_objects(Bucket=bucket, Delete={"Objects": batch})

    def put_text(self, bucket: str, key: str, text: str) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))

    def get_text(self, bucket: str, key: str) -> str:
        """Read an object's text body.

        Raises ``KeyError`` when the object does not exist, matching the
        ``StorageAdapter`` contract used by the other adapters (S3 itself
        signals this with a botocore ``NoSuchKey``/404 ``ClientError``).
        This lets callers such as the audit log treat "no object yet" as
        an empty read rather than a hard failure on first write.
        """
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                raise KeyError(key) from exc
            raise
        return response["Body"].read().decode("utf-8")
