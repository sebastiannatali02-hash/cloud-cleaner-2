"""Tests for scale (bulk delete) and savings-depth (transitions + --as-of)."""

from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.adapters.s3 import S3Adapter
from cloudcleaner.bulk import bulk_delete
from cloudcleaner.config import Config, QuarantineSettings, Rule
from cloudcleaner.models import ScanResult, StorageObject
from cloudcleaner.pricing import PricingModel
from cloudcleaner.transitions import recommend_transitions

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
GIB = 1024**3


def obj(key, age_days=0, size=1024, storage_class="STANDARD"):
    return StorageObject(
        key=key,
        size_bytes=size,
        last_modified=NOW - timedelta(days=age_days),
        storage_class=storage_class,
    )


# --- Scale: bulk_delete ---------------------------------------------------


class TestBulkDelete:
    def test_uses_delete_many_when_available(self):
        adapter = MemoryAdapter()
        adapter.seed("b", [obj("a"), obj("b"), obj("c")])
        n = bulk_delete(adapter, "b", ["a", "c"])
        assert n == 2
        assert sorted(adapter.buckets["b"]) == ["b"]

    def test_falls_back_to_per_key_delete(self):
        """An adapter with no delete_many still works via delete()."""

        class OnlyDelete:
            def __init__(self):
                self.deleted = []

            def delete(self, bucket, key):
                self.deleted.append((bucket, key))

        adapter = OnlyDelete()
        n = bulk_delete(adapter, "b", ["x", "y", "z"])
        assert n == 3
        assert adapter.deleted == [("b", "x"), ("b", "y"), ("b", "z")]
        assert not hasattr(adapter, "delete_many")

    def test_s3_delete_many_batches_by_1000(self, monkeypatch):
        """S3Adapter.delete_many chunks keys into <=1000-key requests."""

        class FakeClient:
            def __init__(self):
                self.calls = []

            def delete_objects(self, Bucket, Delete):
                self.calls.append((Bucket, [o["Key"] for o in Delete["Objects"]]))

        fake = FakeClient()
        monkeypatch.setattr(
            "boto3.client", lambda *a, **k: fake
        )
        adapter = S3Adapter()
        keys = [f"k{i}" for i in range(2500)]
        adapter.delete_many("bucket", keys)
        assert [len(batch) for _, batch in fake.calls] == [1000, 1000, 500]
        assert all(b == "bucket" for b, _ in fake.calls)
        # every key made it into exactly one request
        flat = [k for _, batch in fake.calls for k in batch]
        assert flat == keys

    def test_bulk_delete_dispatches_to_s3_delete_many(self, monkeypatch):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def delete_objects(self, Bucket, Delete):
                self.calls.append([o["Key"] for o in Delete["Objects"]])

        fake = FakeClient()
        monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
        adapter = S3Adapter()
        n = bulk_delete(adapter, "bucket", ["a", "b"])
        assert n == 2
        assert fake.calls == [["a", "b"]]


# --- Savings-depth: recommend_transitions ---------------------------------


class TestRecommendTransitions:
    def test_glacier_transition_math(self):
        result = ScanResult(bucket="b")
        # 100 GiB of STANDARD scanned
        result.scanned_by_class = {"STANDARD": 100 * GIB}
        pricing = PricingModel()

        rec = recommend_transitions(result, pricing, target_class="GLACIER")

        assert rec.target_class == "GLACIER"
        assert rec.standard_bytes == 100 * GIB
        # STANDARD first tier is 0.023/GB, GLACIER is 0.0036/GB
        assert rec.current_monthly == pytest.approx(100 * 0.023)
        assert rec.after_monthly == pytest.approx(100 * 0.0036)
        assert rec.monthly == pytest.approx(100 * (0.023 - 0.0036))
        assert rec.yearly == pytest.approx(rec.monthly * 12)

    def test_no_standard_data_zero_savings(self):
        result = ScanResult(bucket="b")
        result.scanned_by_class = {"GLACIER": 10 * GIB}
        rec = recommend_transitions(result, PricingModel())
        assert rec.standard_bytes == 0
        assert rec.monthly == pytest.approx(0.0)

    def test_deep_archive_target(self):
        result = ScanResult(bucket="b")
        result.scanned_by_class = {"STANDARD": 10 * GIB}
        rec = recommend_transitions(result, PricingModel(), target_class="DEEP_ARCHIVE")
        assert rec.after_monthly == pytest.approx(10 * 0.00099)


# --- Savings/sales: --as-of future preview --------------------------------


class TestAsOf:
    def _config(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text(
            "provider: memory\n"
            "bucket: b\n"
            "rules:\n"
            "  - name: old-logs\n"
            "    keywords: [log]\n"
            "    older_than: 90d\n"
        )
        return str(path)

    def test_as_of_makes_not_yet_old_object_a_candidate(self):
        # Object is 30 days old relative to NOW: not yet older than 90d.
        config = Config(
            provider="memory",
            bucket="b",
            rules=[Rule(name="old-logs", keywords=["log"], older_than="90d")],
            quarantine=QuarantineSettings(retention_days=30),
        )
        from cloudcleaner.rules import RuleEngine

        o = obj("app.log", age_days=30)

        # As of now: not a candidate.
        engine_now = RuleEngine(config, now=NOW)
        assert engine_now.scan([o]).candidate_count == 0

        # As of ~90 days later: the same object is now older than 90d.
        future = NOW + timedelta(days=90)
        engine_future = RuleEngine(config, now=future)
        assert engine_future.scan([o]).candidate_count == 1

    def test_cli_as_of_promotes_object(self, tmp_path, capsys, monkeypatch):
        from cloudcleaner import cli
        from cloudcleaner.adapters import memory as memory_mod

        cfg = self._config(tmp_path)

        # Seed a memory adapter that the CLI will fetch via get_adapter.
        shared = MemoryAdapter()
        shared.seed("b", [obj("app.log", age_days=30)])
        monkeypatch.setattr(cli, "get_adapter", lambda config: shared)

        # Without --as-of: object is only 30d old, not a candidate.
        assert cli.main(["scan", "--config", cfg]) == 0
        assert "No objects matched" in capsys.readouterr().out

        # With --as-of a future date: object becomes a candidate.
        future = (NOW + timedelta(days=120)).date().isoformat()
        assert cli.main(["scan", "--config", cfg, "--as-of", future]) == 0
        out = capsys.readouterr().out
        assert "app.log" in out
