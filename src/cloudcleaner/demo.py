"""Offline demo on a simulated bucket — no AWS credentials needed.

Useful to show a prospect (or investor) what the scan report looks
like before pointing the tool at their real storage.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from cloudcleaner.adapters.memory import MemoryAdapter
from cloudcleaner.config import Config, QuarantineSettings, Rule
from cloudcleaner.models import StorageObject
from cloudcleaner.report import compute_savings, text_report
from cloudcleaner.rules import RuleEngine

GIB = 1024**3


def demo_config() -> Config:
    return Config(
        provider="memory",
        bucket="acme-corp-data",
        rules=[
            Rule(name="stale-logs", keywords=["log"], older_than="90d"),
            Rule(name="tmp-artifacts", suffixes=[".tmp", ".bak", ".old"]),
            Rule(name="invoices-past-retention", prefixes=["invoices/"], older_than="10y"),
            Rule(name="big-old-backups", prefixes=["backups/"], older_than="1y", min_size="1GB"),
        ],
        exclude=["legal-hold/"],
        quarantine=QuarantineSettings(),
    )


def seed_objects(now: datetime) -> list[StorageObject]:
    rng = random.Random(42)

    def obj(key: str, gib: float, age_days: int) -> StorageObject:
        return StorageObject(
            key=key,
            size_bytes=int(gib * GIB),
            last_modified=now - timedelta(days=age_days),
        )

    objects = [
        obj(f"logs/app/2024/app-{i:03}.log", rng.uniform(0.5, 3), 400 + i) for i in range(40)
    ]
    objects += [obj(f"logs/app/2026/app-{i:03}.log", rng.uniform(0.1, 1), i) for i in range(30)]
    objects += [obj(f"build/cache-{i}.tmp", rng.uniform(1, 6), rng.randint(10, 900)) for i in range(25)]
    objects += [obj(f"invoices/{2008 + i // 10}/invoice-{i:04}.pdf", 0.002, 3600 + i * 30) for i in range(60)]
    objects += [obj(f"invoices/2025/invoice-{i:04}.pdf", 0.002, 100 + i) for i in range(40)]
    objects += [obj(f"backups/db/full-{i:02}.dump", rng.uniform(20, 80), 380 + i * 15) for i in range(12)]
    objects += [obj(f"backups/db/recent-{i:02}.dump", rng.uniform(20, 40), 5 + i) for i in range(4)]
    objects += [obj(f"legal-hold/case-102/evidence-{i}.zip", 5, 4000) for i in range(6)]
    objects += [obj(f"projects/active/model-{i}.bin", rng.uniform(2, 10), rng.randint(1, 60)) for i in range(20)]
    return objects


def run_demo() -> None:
    now = datetime.now(timezone.utc)
    config = demo_config()
    adapter = MemoryAdapter()
    adapter.seed(config.bucket, seed_objects(now))

    engine = RuleEngine(config, now=now)
    result = engine.scan(adapter.list_objects(config.bucket))
    savings = compute_savings(result, config.pricing)

    print("cloudcleaner demo — simulated bucket, no real data touched\n")
    print(text_report(result, savings, config.quarantine.retention_days, limit=10))
