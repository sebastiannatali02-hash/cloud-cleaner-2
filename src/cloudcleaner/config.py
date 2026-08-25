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

from cloudcleaner.pricing import PricingModel

QUARANTINE_PREFIX_DEFAULT = "_cloudcleaner/quarantine/"
RETENTION_DAYS_DEFAULT = 30

_RELATIVE_PERIOD = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_HUMAN_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$", re.IGNORECASE)

_PERIOD_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
_SIZE_FACTORS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}

# The leaf condition keys a rule (or a nested match node) may carry. These map
# 1:1 to the condition fields on ``Rule`` and to the predicate builders in
# ``rules._CONDITION_BUILDERS`` — kept here as the single source of truth so the
# engine and the config validator agree on what a "leaf" looks like.
RULE_CONDITION_FIELDS = frozenset(
    {
        "keywords",
        "match_regex",
        "prefixes",
        "suffixes",
        "older_than",
        "min_size",
        "storage_classes",
    }
)

# Logical operators allowed inside a ``match`` tree.
MATCH_GROUP_OPS = frozenset({"any_of", "all_of", "none_of", "not"})


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


def _validate_conditions(conditions: dict, ctx: str) -> None:
    """Validate a bag of leaf conditions (regex compiles, sizes/periods parse)."""
    regex = conditions.get("match_regex")
    if regex:
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"{ctx}: invalid regex: {exc}") from exc
    if conditions.get("older_than") is not None:
        parse_cutoff(conditions["older_than"], datetime.now(timezone.utc))
    if conditions.get("min_size") is not None:
        parse_size(conditions["min_size"])


def _validate_match_node(node: object, ctx: str) -> None:
    """Recursively validate a ``match`` tree.

    A node is either a *leaf* (a mapping of condition fields, ANDed) or a
    *logical group* (a mapping containing ``any_of`` / ``all_of`` / ``none_of``
    / ``not``). The two forms may not be mixed in one mapping.
    """
    if not isinstance(node, dict):
        raise ConfigError(f"{ctx}: match node must be a mapping, got {type(node).__name__}")
    keys = set(node)
    group_keys = keys & MATCH_GROUP_OPS
    cond_keys = keys & RULE_CONDITION_FIELDS
    unknown = keys - MATCH_GROUP_OPS - RULE_CONDITION_FIELDS
    if unknown:
        raise ConfigError(f"{ctx}: unknown keys in match node: {sorted(unknown)}")
    if group_keys and cond_keys:
        raise ConfigError(
            f"{ctx}: a match node is either a condition leaf or a logical group, not both "
            f"(saw both {sorted(cond_keys)} and {sorted(group_keys)})"
        )
    if not group_keys and not cond_keys:
        raise ConfigError(f"{ctx}: empty match node")
    if not group_keys:
        _validate_conditions(node, ctx)
        return
    for op in group_keys:
        children = node[op]
        if op == "not":
            # 'not' negates a single node or (implicitly OR-ed) list of nodes.
            child_list = children if isinstance(children, list) else [children]
            if not child_list:
                raise ConfigError(f"{ctx}.not: must not be empty")
            for i, child in enumerate(child_list):
                _validate_match_node(child, f"{ctx}.not[{i}]")
        else:
            if not isinstance(children, list) or not children:
                raise ConfigError(f"{ctx}.{op}: must be a non-empty list of match nodes")
            for i, child in enumerate(children):
                _validate_match_node(child, f"{ctx}.{op}[{i}]")


@dataclass
class Rule:
    """One cleanup rule.

    The flat condition fields (``keywords``/``prefixes``/``suffixes``/... ) all
    AND together, as before. Additionally a rule may carry:

    * ``match`` – a nested boolean tree (``any_of``/``all_of``/``none_of``/``not``)
      whose leaves are the same condition fields. When present it is ANDed with
      any flat conditions.
    * ``keep_newest`` + ``group_by`` – within each group produced by ``group_by``,
      the ``keep_newest`` most-recently-modified objects are protected and the
      rest become candidates. Any flat conditions / ``match`` tree scope which
      objects the rule considers.
    """

    name: str
    keywords: list[str] = field(default_factory=list)
    match_regex: str | None = None
    prefixes: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    older_than: str | None = None
    min_size: int | str | None = None
    storage_classes: list[str] = field(default_factory=list)
    match: dict | None = None
    keep_newest: int | None = None
    group_by: str | None = None

    def _has_flat_conditions(self) -> bool:
        return any(
            [self.keywords, self.match_regex, self.prefixes, self.suffixes,
             self.older_than, self.min_size, self.storage_classes]
        )

    def __post_init__(self) -> None:
        if self.match is not None:
            _validate_match_node(self.match, f"rule {self.name!r}.match")

        if self.keep_newest is not None:
            if isinstance(self.keep_newest, bool) or not isinstance(self.keep_newest, int):
                raise ConfigError(f"rule {self.name!r}: keep_newest must be an integer")
            if self.keep_newest < 0:
                raise ConfigError(f"rule {self.name!r}: keep_newest must be >= 0")
            if not self.group_by:
                raise ConfigError(f"rule {self.name!r}: keep_newest requires group_by")
            _validate_group_by(self.group_by, f"rule {self.name!r}")
        elif self.group_by is not None:
            raise ConfigError(f"rule {self.name!r}: group_by is only valid together with keep_newest")

        # A keep_newest rule selects "everything but the newest N per group",
        # which is a bounded selection, so it does not need extra conditions.
        if not self._has_flat_conditions() and self.match is None and self.keep_newest is None:
            raise ConfigError(f"rule {self.name!r} has no conditions; it would match everything")

        _validate_conditions(
            {
                "match_regex": self.match_regex,
                "older_than": self.older_than,
                "min_size": self.min_size,
            },
            f"rule {self.name!r}",
        )


def _validate_group_by(spec: str, ctx: str) -> None:
    """Validate a ``group_by`` spec: ``prefix:<N>`` or a regex."""
    if not isinstance(spec, str) or not spec:
        raise ConfigError(f"{ctx}: group_by must be a non-empty string")
    if spec.startswith("prefix:"):
        depth = spec[len("prefix:"):]
        if not depth.isdigit() or int(depth) < 1:
            raise ConfigError(f"{ctx}: group_by 'prefix:<N>' needs a positive integer, got {spec!r}")
        return
    try:
        re.compile(spec)
    except re.error as exc:
        raise ConfigError(f"{ctx}: group_by is not 'prefix:<N>' nor a valid regex: {exc}") from exc


@dataclass
class QuarantineSettings:
    prefix: str = QUARANTINE_PREFIX_DEFAULT
    retention_days: int = RETENTION_DAYS_DEFAULT
    bucket: str | None = None  # defaults to the scanned bucket
    # Raw blast-radius guardrail limits (max_fraction/max_objects/max_bytes).
    # Passed as-is to guardrail.GuardrailLimits.from_dict, which tolerates
    # missing/None keys; an empty dict means "no limits".
    guardrail: dict = field(default_factory=dict)


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
    pricing: PricingModel = field(default_factory=PricingModel)


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
        guardrail=q_raw.get("guardrail") or {},
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
        pricing=PricingModel(overrides=overrides, currency=str(p_raw.get("currency", "USD"))),
    )
