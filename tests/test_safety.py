"""Trust & safety tests around the quarantine/purge lifecycle.

Covers manifest integrity checksums, the append-only audit log, and the
interactive purge confirmation.
"""

import json
from datetime import timedelta

import pytest

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.cli import main
from cloudcleaner.config import load_config
from cloudcleaner.quarantine import (
    Manifest,
    ManifestEntry,
    ManifestIntegrityError,
    QuarantineManager,
)
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


def _sample_manifest():
    return Manifest(
        batch_id="20260715T120000Z",
        bucket="test-bucket",
        quarantine_bucket="test-bucket",
        created_at=NOW.isoformat(),
        purge_after=(NOW + timedelta(days=30)).isoformat(),
        entries=[
            ManifestEntry("app/server.log", "q/app/server.log", 100, "old-logs"),
            ManifestEntry("build/cache.tmp", "q/build/cache.tmp", 50, "tmp-files"),
        ],
    )


class TestManifestChecksum:
    def test_checksum_present_and_roundtrips(self):
        manifest = _sample_manifest()
        text = manifest.to_json()
        data = json.loads(text)
        assert "checksum" in data
        assert len(data["checksum"]) == 64  # sha-256 hex digest

        restored = Manifest.from_json(text)
        assert restored.integrity_verified is True
        assert restored.checksum == manifest.checksum
        assert [e.original_key for e in restored.entries] == [
            "app/server.log",
            "build/cache.tmp",
        ]

    def test_tampered_entries_raise_integrity_error(self):
        data = json.loads(_sample_manifest().to_json())
        # Flip a size without recomputing the checksum -> tamper.
        data["entries"][0]["size_bytes"] = 999999
        with pytest.raises(ManifestIntegrityError):
            Manifest.from_json(json.dumps(data))

    def test_tampered_checksum_raises_integrity_error(self):
        data = json.loads(_sample_manifest().to_json())
        data["checksum"] = "0" * 64
        with pytest.raises(ManifestIntegrityError):
            Manifest.from_json(json.dumps(data))

    def test_legacy_manifest_without_checksum_accepted_but_unverified(self):
        data = json.loads(_sample_manifest().to_json())
        data.pop("checksum")
        restored = Manifest.from_json(json.dumps(data))
        assert restored.integrity_verified is False
        assert len(restored.entries) == 2

    def test_purge_surfaces_integrity_error(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        # Corrupt the stored manifest on the backend.
        key = manager._manifest_key(manifest.batch_id)
        data = json.loads(adapter.get_text(manager.q_bucket, key))
        data["entries"][0]["size_bytes"] = 12345
        adapter.put_text(manager.q_bucket, key, json.dumps(data))

        later = QuarantineManager(adapter, config, now=NOW + timedelta(days=31))
        with pytest.raises(ManifestIntegrityError):
            later.expired_batches()  # list_batches reads + verifies


class TestAuditLog:
    def test_audit_accrues_across_actions(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manifest = manager.quarantine(scan(config, adapter).candidates)

        entries = manager.read_audit_log()
        assert len(entries) == 1
        first = entries[0]
        assert first["action"] == "quarantine"
        assert first["batch_id"] == manifest.batch_id
        assert first["object_count"] == 2
        assert first["total_bytes"] == 150
        assert set(first["keys"]) == {"app/server.log", "build/cache.tmp"}
        assert first["timestamp"] == NOW.isoformat()  # injected now, not wall clock

        manager.restore(manifest.batch_id, keys=["app/server.log"])
        later = QuarantineManager(adapter, config, now=NOW + timedelta(days=31))
        later.purge(later.expired_batches())

        actions = [e["action"] for e in later.read_audit_log()]
        assert actions == ["quarantine", "restore", "purge"]

    def test_audit_survives_purge(self, config, adapter):
        manager = QuarantineManager(adapter, config, now=NOW)
        manager.quarantine(scan(config, adapter).candidates)
        later = QuarantineManager(adapter, config, now=NOW + timedelta(days=31))
        later.purge(later.expired_batches())
        # No batches remain, but the audit trail is intact.
        assert later.list_batches() == []
        assert len(later.read_audit_log()) >= 2


PURGE_CONFIG = """
provider: memory
bucket: test-bucket
rules:
  - name: logs
    keywords: [log]
    older_than: 90d
quarantine:
  retention_days: 30
"""


class TestPurgeConfirmation:
    def test_purge_refuses_without_confirmation_non_interactive(
        self, tmp_path, monkeypatch, capsys
    ):
        # Seed a bucket via a shared adapter that get_adapter will return.
        adapter = MemoryAdapter()
        adapter.seed("test-bucket", [obj("app/server.log", age_days=400, size=100)])
        monkeypatch.setattr("cloudcleaner.cli.get_adapter", lambda config: adapter)

        cfg = tmp_path / "rules.yaml"
        cfg.write_text(PURGE_CONFIG)

        # Quarantine first (force apply), moving the object into quarantine.
        assert main(["quarantine", "--config", str(cfg), "--apply"]) == 0

        # Force stdin to look non-interactive.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        rc = main(["purge", "--config", str(cfg), "--apply", "--force"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--yes" in err
        # Object still quarantined (not deleted).
        manager = QuarantineManager(adapter, load_config(str(cfg)))
        assert manager.list_batches(), "batch should still exist after refused purge"

    def test_purge_with_yes_flag_deletes(self, tmp_path, monkeypatch, capsys):
        adapter = MemoryAdapter()
        adapter.seed("test-bucket", [obj("app/server.log", age_days=400, size=100)])
        monkeypatch.setattr("cloudcleaner.cli.get_adapter", lambda config: adapter)

        cfg = tmp_path / "rules.yaml"
        cfg.write_text(PURGE_CONFIG)

        assert main(["quarantine", "--config", str(cfg), "--apply"]) == 0
        # Non-interactive but --yes provided -> proceeds.
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        rc = main(["purge", "--config", str(cfg), "--apply", "--force", "--yes"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Permanently deleted" in out
