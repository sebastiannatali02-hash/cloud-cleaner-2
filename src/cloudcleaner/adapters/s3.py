"""AWS S3 adapter.

Credentials come from the standard boto3 chain (env vars, shared
credentials file, instance profile). ``endpoint_url`` allows pointing
at S3-compatible storage (MinIO, Cloudflare R2, ...).
"""

from __future__ import annotations

from typing import Iterator

import boto3

from ..models import StorageObject


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

    def put_text(self, bucket: str, key: str, text: str) -> None:
        self._client.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))

    def get_text(self, bucket: str, key: str) -> str:
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read().decode("utf-8")
