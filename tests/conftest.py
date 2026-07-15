from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.config import Config, QuarantineSettings, Rule
from cloudcleaner.models import StorageObject

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
GIB = 1024**3


def obj(key: str, age_days: int = 0, size: int = 1024, storage_class: str = "STANDARD"):
    return StorageObject(
        key=key,
        size_bytes=size,
        last_modified=NOW - timedelta(days=age_days),
        storage_class=storage_class,
    )


@pytest.fixture
def config() -> Config:
    return Config(
        provider="memory",
        bucket="test-bucket",
        rules=[
            Rule(name="old-logs", keywords=["log"], older_than="90d"),
            Rule(name="tmp-files", suffixes=[".tmp"]),
            Rule(name="invoices-prescribed", prefixes=["invoices/"], older_than="10y"),
            Rule(name="big-old", older_than="1y", min_size="1GB"),
        ],
        exclude=["legal-hold/", "*.db"],
        quarantine=QuarantineSettings(retention_days=30),
    )
