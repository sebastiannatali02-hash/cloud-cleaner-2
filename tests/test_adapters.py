"""Unit tests for the GCS and Azure storage adapters.

The real ``google-cloud-storage`` and ``azure-storage-blob`` SDKs are
NOT installed. Instead we build small fake client objects that mimic the
slice of each SDK's surface the adapters touch, and inject them via the
adapters' ``client=`` constructor argument. This exercises the adapter
logic (list_objects -> StorageObject mapping, copy, delete,
put_text/get_text) without any network or SDK dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cloudcleaner.adapters.azure import AzureAdapter
from cloudcleaner.adapters.gcs import GCSAdapter
from cloudcleaner.models import StorageObject

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# GCS fakes
# --------------------------------------------------------------------------- #
class FakeGCSBlob:
    def __init__(self, store, bucket, name):
        self._store = store
        self._bucket = bucket
        self.name = name

    def _entry(self):
        return self._store[self._bucket][self.name]

    @property
    def size(self):
        return self._entry()["size"]

    @property
    def updated(self):
        return self._entry()["updated"]

    @property
    def storage_class(self):
        return self._entry()["storage_class"]

    def upload_from_string(self, text, content_type=None):
        data = text.encode("utf-8") if isinstance(text, str) else text
        self._store.setdefault(self._bucket, {})[self.name] = {
            "data": data,
            "size": len(data),
            "updated": NOW,
            "storage_class": "STANDARD",
        }

    def download_as_bytes(self):
        return self._entry()["data"]

    def delete(self):
        del self._store[self._bucket][self.name]


class FakeGCSBucket:
    def __init__(self, store, name):
        self._store = store
        self.name = name

    def blob(self, name):
        return FakeGCSBlob(self._store, self.name, name)

    def copy_blob(self, blob, destination_bucket, new_name):
        # Mirrors google.cloud.storage.Bucket.copy_blob.
        entry = dict(self._store[blob._bucket][blob.name])
        self._store.setdefault(destination_bucket.name, {})[new_name] = entry


class FakeGCSClient:
    """Mimics google.cloud.storage.Client for the calls the adapter makes."""

    def __init__(self, store=None):
        self._store = store if store is not None else {}

    def bucket(self, name):
        return FakeGCSBucket(self._store, name)

    def list_blobs(self, bucket, prefix=""):
        for name in sorted(self._store.get(bucket, {})):
            if name.startswith(prefix):
                yield FakeGCSBlob(self._store, bucket, name)


@pytest.fixture
def gcs_store():
    return {
        "src": {
            "logs/a.log": {"data": b"aaa", "size": 3, "updated": NOW, "storage_class": "STANDARD"},
            "logs/b.log": {"data": b"bb", "size": 2, "updated": NOW, "storage_class": "NEARLINE"},
            "data/c.bin": {"data": b"c", "size": 1, "updated": NOW, "storage_class": "STANDARD"},
        }
    }


@pytest.fixture
def gcs(gcs_store):
    return GCSAdapter(client=FakeGCSClient(gcs_store))


def test_gcs_list_objects_maps_to_storage_object(gcs):
    objs = list(gcs.list_objects("src"))
    assert all(isinstance(o, StorageObject) for o in objs)
    assert [o.key for o in objs] == ["data/c.bin", "logs/a.log", "logs/b.log"]
    a = next(o for o in objs if o.key == "logs/a.log")
    assert a.size_bytes == 3
    assert a.last_modified == NOW
    assert a.storage_class == "STANDARD"
    b = next(o for o in objs if o.key == "logs/b.log")
    assert b.storage_class == "NEARLINE"


def test_gcs_list_objects_prefix_filters(gcs):
    keys = [o.key for o in gcs.list_objects("src", prefix="logs/")]
    assert keys == ["logs/a.log", "logs/b.log"]


def test_gcs_copy(gcs, gcs_store):
    gcs.copy("src", "logs/a.log", "dst", "backup/a.log")
    assert gcs_store["dst"]["backup/a.log"]["data"] == b"aaa"


def test_gcs_delete(gcs, gcs_store):
    gcs.delete("src", "logs/a.log")
    assert "logs/a.log" not in gcs_store["src"]


def test_gcs_put_and_get_text(gcs):
    gcs.put_text("src", "notes.txt", "hello gcs")
    assert gcs.get_text("src", "notes.txt") == "hello gcs"


# --------------------------------------------------------------------------- #
# Azure fakes
# --------------------------------------------------------------------------- #
class FakeAzureBlobProps:
    def __init__(self, name, size, last_modified, blob_tier):
        self.name = name
        self.size = size
        self.last_modified = last_modified
        self.blob_tier = blob_tier


class FakeAzureDownload:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class FakeAzureBlobClient:
    def __init__(self, store, container, blob):
        self._store = store
        self._container = container
        self._blob = blob

    @property
    def url(self):
        return f"https://fake/{self._container}/{self._blob}"

    def upload_blob(self, data, overwrite=False):
        data = data.encode("utf-8") if isinstance(data, str) else data
        self._store.setdefault(self._container, {})[self._blob] = {
            "data": data,
            "size": len(data),
            "last_modified": NOW,
            "blob_tier": "Hot",
        }

    def download_blob(self):
        return FakeAzureDownload(self._store[self._container][self._blob]["data"])

    def delete_blob(self):
        del self._store[self._container][self._blob]

    def start_copy_from_url(self, url):
        # url is https://fake/<container>/<blob>
        _, _, _, src_container, src_blob = url.split("/", 4)
        entry = dict(self._store[src_container][src_blob])
        self._store.setdefault(self._container, {})[self._blob] = entry


class FakeAzureContainerClient:
    def __init__(self, store, container):
        self._store = store
        self._container = container

    def list_blobs(self, name_starts_with=""):
        for name in sorted(self._store.get(self._container, {})):
            if name.startswith(name_starts_with):
                e = self._store[self._container][name]
                yield FakeAzureBlobProps(name, e["size"], e["last_modified"], e["blob_tier"])


class FakeBlobServiceClient:
    """Mimics azure.storage.blob.BlobServiceClient."""

    def __init__(self, store=None):
        self._store = store if store is not None else {}

    def get_container_client(self, container):
        return FakeAzureContainerClient(self._store, container)

    def get_blob_client(self, container, blob):
        return FakeAzureBlobClient(self._store, container, blob)


@pytest.fixture
def azure_store():
    return {
        "src": {
            "logs/a.log": {"data": b"aaa", "size": 3, "last_modified": NOW, "blob_tier": "Hot"},
            "logs/b.log": {"data": b"bb", "size": 2, "last_modified": NOW, "blob_tier": "Cool"},
            "data/c.bin": {"data": b"c", "size": 1, "last_modified": NOW, "blob_tier": "Hot"},
        }
    }


@pytest.fixture
def azure(azure_store):
    return AzureAdapter(client=FakeBlobServiceClient(azure_store))


def test_azure_list_objects_maps_to_storage_object(azure):
    objs = list(azure.list_objects("src"))
    assert all(isinstance(o, StorageObject) for o in objs)
    assert [o.key for o in objs] == ["data/c.bin", "logs/a.log", "logs/b.log"]
    a = next(o for o in objs if o.key == "logs/a.log")
    assert a.size_bytes == 3
    assert a.last_modified == NOW
    assert a.storage_class == "Hot"
    b = next(o for o in objs if o.key == "logs/b.log")
    assert b.storage_class == "Cool"


def test_azure_list_objects_prefix_filters(azure):
    keys = [o.key for o in azure.list_objects("src", prefix="logs/")]
    assert keys == ["logs/a.log", "logs/b.log"]


def test_azure_copy(azure, azure_store):
    azure.copy("src", "logs/a.log", "dst", "backup/a.log")
    assert azure_store["dst"]["backup/a.log"]["data"] == b"aaa"


def test_azure_delete(azure, azure_store):
    azure.delete("src", "logs/a.log")
    assert "logs/a.log" not in azure_store["src"]


def test_azure_put_and_get_text(azure):
    azure.put_text("src", "notes.txt", "hello azure")
    assert azure.get_text("src", "notes.txt") == "hello azure"


# --------------------------------------------------------------------------- #
# Factory registration
# --------------------------------------------------------------------------- #
def test_factories_registered():
    from cloudcleaner.adapters import _REGISTRY

    assert "gcs" in _REGISTRY
    assert "azure" in _REGISTRY
