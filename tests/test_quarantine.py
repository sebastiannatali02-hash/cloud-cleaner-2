from datetime import timedelta

import pytest

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.quarantine import QuarantineManager
from cloudcleaner.rules import RuleEngine

from conftest import NOW, obj


@pytest.fixture
def adapter(config):
    a = MemoryAdapter()
    a.seed(
        config.bucket,
        [
            obj("app/server.log", age_days=120, size=100),
            obj("build/cache.tmp", size=50),
            obj("projects/keep.txt", age_days=5, size=10),
        ],
    )
    return a


def scan(config, adapter):
    engine = RuleEngine(config, now=NOW)
    return engine.scan(adapter.list_objects(config.bucket))


def keys(adapter, bucket):
    return {o.key for o in adapter.list_objects(bucket)}


class TestQuarantineLifecycle:
    def test_quarantine_moves_objects_and_writes_manifest(self, config, adapter):
        result = scan(config, adapter)
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(result.candidates)

        assert len(manifest.entries) == 2
        assert manifest.total_bytes == 150
        remaining = keys(adapter, config.bucket)
        assert "app/server.log" not in remaining
        assert "build/cache.tmp" not in remaining
        assert "projects/keep.txt" in remaining
        q_prefix = f"{config.quarantine.prefix}{manifest.batch_id}/"
        assert f"{q_prefix}objects/app/server.log" in remaining
        assert f"{q_prefix}manifest.json" in remaining

        # a second scan must not re-select the quarantined copies
        assert scan(config, adapter).candidate_count == 0

    def test_purge_only_after_retention(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        assert manager.expired_batches() == []

        later = QuarantineManager(adapter, config, now=NOW + timedelta(days=31))
        expired = later.expired_batches()
        assert [m.batch_id for m in expired] == [manifest.batch_id]

        freed = later.purge(expired)
        assert freed == 150
        assert later.list_batches() == []
        assert not any(k.startswith(config.quarantine.prefix) for k in keys(adapter, config.bucket))

    def test_restore_whole_batch(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        restored = manager.restore(manifest.batch_id)
        assert {e.original_key for e in restored} == {"app/server.log", "build/cache.tmp"}
        remaining = keys(adapter, config.bucket)
        assert "app/server.log" in remaining
        assert "build/cache.tmp" in remaining
        assert manager.list_batches() == []  # manifest removed once batch is empty

    def test_restore_single_key_keeps_manifest(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        restored = manager.restore(manifest.batch_id, keys=["app/server.log"])
        assert [e.original_key for e in restored] == ["app/server.log"]
        batches = manager.list_batches()
        assert len(batches) == 1
        assert [e.original_key for e in batches[0].entries] == ["build/cache.tmp"]

    def test_restore_unknown_batch_or_key(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        with pytest.raises(KeyError):
            manager.restore("nope")
        manifest = manager.quarantine(scan(config, adapter).candidates)
        with pytest.raises(KeyError, match="not found"):
            manager.restore(manifest.batch_id, keys=["does/not/exist"])

    def test_separate_quarantine_bucket(self, config, adapter):
        config.quarantine.bucket = "quarantine-bucket"
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        assert "app/server.log" not in keys(adapter, config.bucket)
        q_keys = keys(adapter, "quarantine-bucket")
        assert f"{config.quarantine.prefix}{manifest.batch_id}/objects/app/server.log" in q_keys

        manager.restore(manifest.batch_id)
        assert "app/server.log" in keys(adapter, config.bucket)
