"""Configuration loading and validation.

The whole tool is driven by a single YAML file: which bucket to scan,
the cleanup rules, exclusions that must never be touched, quarantine
behaviour and optional pricing overrides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

QUARANTINE_PREFIX_DEFAULT = "_cloudcleaner/quarantine/"
RETENTION_DAYS_DEFAULT = 30

_RELATIVE_PERIOD = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_HUMAN_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$", re.IGNORECASE)

_PERIOD_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
_SIZE_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class ConfigError(Exception):
    """Raised when the YAML configuration is invalid."""


def parse_cutoff(value: str | datetime, now: datetime) -> datetime:
    """Turn an ``older_than`` value into an absolute UTC cutoff.

    Accepts an ISO date/datetime ("2016-07-15") or a relative period:
    "90d", "8w", "6m" (months as 30 days), "10y" (years as 365 days) —
    the latter is how a legal retention/prescription period is expressed.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    text = str(value).strip()
    m = _RELATIVE_PERIOD.match(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        return now - timedelta(days=amount * _PERIOD_DAYS[unit])
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ConfigError(
            f"older_than {value!r} is neither a period like '90d'/'10y' nor an ISO date"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_size(value: int | str) -> int:
    """Turn a human size ("500KB", "1.5GB", plain bytes) into bytes."""
    if isinstance(value, int):
        return value
    m = _HUMAN_SIZE.match(str(value).strip())
    if not m:
        raise ConfigError(f"min_size {value!r} is not a valid size (try '500KB', '1GB')")
    return int(float(m.group(1)) * _SIZE_FACTORS[m.group(2).lower()])


@dataclass
class Rule:
    """One cleanup rule. All set conditions must hold (AND) for a match."""

    name: str
    keywords: list[str] = field(default_factory=list)
    match_regex: str | None = None
    prefixes: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    older_than: str | None = None
    min_size: int | str | None = None
    storage_classes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not any(
            [self.keywords, self.match_regex, self.prefixes, self.suffixes,
             self.older_than, self.min_size, self.storage_classes]
        ):
            raise ConfigError(f"rule {self.name!r} has no conditions; it would match everything")
        if self.match_regex:
            try:
                re.compile(self.match_regex)
            except re.error as exc:
                raise ConfigError(f"rule {self.name!r}: invalid regex: {exc}") from exc
        if self.older_than is not None:
            parse_cutoff(self.older_than, datetime.now(timezone.utc))
        if self.min_size is not None:
            parse_size(self.min_size)


@dataclass
class QuarantineSettings:
    prefix: str = QUARANTINE_PREFIX_DEFAULT
    retention_days: int = RETENTION_DAYS_DEFAULT
    bucket: str | None = None  # defaults to the scanned bucket


@dataclass
class Config:
    provider: str
    bucket: str
    rules: list[Rule]
    exclude: list[str] = field(default_factory=list)
    prefix: str = ""
    region: str | None = None
    endpoint_url: str | None = None
    quarantine: QuarantineSettings = field(default_factory=QuarantineSettings)
    pricing_overrides: dict[str, float] = field(default_factory=dict)
    currency: str = "USD"


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a YAML mapping at the top level")

    for required in ("bucket", "rules"):
        if required not in raw:
            raise ConfigError(f"{path}: missing required key {required!r}")

    rules_raw = raw["rules"]
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError(f"{path}: 'rules' must be a non-empty list")

    rules = []
    for i, entry in enumerate(rules_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: rule #{i + 1} must be a mapping")
        entry = dict(entry)
        entry.setdefault("name", f"rule-{i + 1}")
        known = {f for f in Rule.__dataclass_fields__}
        unknown = set(entry) - known
        if unknown:
            raise ConfigError(f"{path}: rule {entry['name']!r} has unknown keys: {sorted(unknown)}")
        rules.append(Rule(**entry))

    q_raw = raw.get("quarantine") or {}
    quarantine = QuarantineSettings(
        prefix=q_raw.get("prefix", QUARANTINE_PREFIX_DEFAULT),
        retention_days=int(q_raw.get("retention_days", RETENTION_DAYS_DEFAULT)),
        bucket=q_raw.get("bucket"),
    )
    if not quarantine.prefix.endswith("/"):
        quarantine.prefix += "/"
    if quarantine.retention_days < 0:
        raise ConfigError(f"{path}: quarantine.retention_days must be >= 0")

    p_raw = raw.get("pricing") or {}
    overrides = {str(k).upper(): float(v) for k, v in (p_raw.get("overrides") or {}).items()}

    return Config(
        provider=str(raw.get("provider", "s3")).lower(),
        bucket=str(raw["bucket"]),
        rules=rules,
        exclude=[str(p) for p in (raw.get("exclude") or [])],
        prefix=str(raw.get("prefix", "")),
        region=raw.get("region"),
        endpoint_url=raw.get("endpoint_url"),
        quarantine=quarantine,
        pricing_overrides=overrides,
        currency=str(p_raw.get("currency", "USD")),
    )
