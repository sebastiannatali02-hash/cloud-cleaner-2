"""Rule engine: decides which objects are cleanup candidates.

An object becomes a candidate when at least one rule matches it
(conditions inside a rule are ANDed). Exclusion patterns always win,
and anything under the quarantine prefix is never re-selected.

Each rule condition compiles to an independent predicate via a builder
in ``_CONDITION_BUILDERS``; supporting a new condition means adding one
builder there (open/closed) rather than editing the matching loop.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from cloudcleaner.config import Config, Rule, parse_cutoff, parse_size
from cloudcleaner.models import Candidate, ScanResult, StorageObject

Predicate = Callable[[StorageObject], bool]
PredicateBuilder = Callable[[Rule, datetime], Predicate | None]


def _keywords(rule: Rule, now: datetime) -> Predicate | None:
    if not rule.keywords:
        return None
    keywords = [k.lower() for k in rule.keywords]
    return lambda obj: any(k in obj.key.lower() for k in keywords)


def _regex(rule: Rule, now: datetime) -> Predicate | None:
    if not rule.match_regex:
        return None
    pattern = re.compile(rule.match_regex)
    return lambda obj: pattern.search(obj.key) is not None


def _prefixes(rule: Rule, now: datetime) -> Predicate | None:
    if not rule.prefixes:
        return None
    prefixes = tuple(rule.prefixes)
    return lambda obj: obj.key.startswith(prefixes)


def _suffixes(rule: Rule, now: datetime) -> Predicate | None:
    if not rule.suffixes:
        return None
    suffixes = tuple(s.lower() for s in rule.suffixes)
    return lambda obj: obj.key.lower().endswith(suffixes)


def _older_than(rule: Rule, now: datetime) -> Predicate | None:
    if rule.older_than is None:
        return None
    cutoff = parse_cutoff(rule.older_than, now)
    return lambda obj: obj.last_modified < cutoff


def _min_size(rule: Rule, now: datetime) -> Predicate | None:
    if rule.min_size is None:
        return None
    min_bytes = parse_size(rule.min_size)
    return lambda obj: obj.size_bytes >= min_bytes


def _storage_classes(rule: Rule, now: datetime) -> Predicate | None:
    if not rule.storage_classes:
        return None
    allowed = frozenset(rule.storage_classes)
    return lambda obj: obj.storage_class in allowed


_CONDITION_BUILDERS: tuple[PredicateBuilder, ...] = (
    _keywords,
    _regex,
    _prefixes,
    _suffixes,
    _older_than,
    _min_size,
    _storage_classes,
)


def compile_rule(rule: Rule, now: datetime) -> list[Predicate]:
    """All active conditions of the rule as predicates (ANDed on match)."""
    return [p for builder in _CONDITION_BUILDERS if (p := builder(rule, now)) is not None]


class ExclusionPolicy:
    """Decides which keys must never be touched, whatever the rules say."""

    def __init__(self, patterns: Iterable[str], quarantine_prefix: str):
        self._patterns = list(patterns)
        self._quarantine_prefix = quarantine_prefix

    def is_excluded(self, key: str) -> bool:
        if key.startswith(self._quarantine_prefix):
            return True
        for pattern in self._patterns:
            # A bare prefix pattern ("legal-hold/") protects the whole subtree;
            # anything else is treated as a glob against the full key.
            if pattern.endswith("/") and key.startswith(pattern):
                return True
            if fnmatch.fnmatch(key, pattern):
                return True
        return False


class RuleEngine:
    def __init__(self, config: Config, now: datetime | None = None):
        self.config = config
        self.now = now or datetime.now(timezone.utc)
        self.exclusions = ExclusionPolicy(config.exclude, config.quarantine.prefix)
        self._compiled: list[tuple[str, list[Predicate]]] = [
            (rule.name, compile_rule(rule, self.now)) for rule in config.rules
        ]

    def is_excluded(self, key: str) -> bool:
        return self.exclusions.is_excluded(key)

    def match(self, obj: StorageObject) -> str | None:
        """Return the name of the first matching rule, or None."""
        if self.exclusions.is_excluded(obj.key):
            return None
        for rule_name, predicates in self._compiled:
            if all(predicate(obj) for predicate in predicates):
                return rule_name
        return None

    def scan(self, objects: Iterable[StorageObject]) -> ScanResult:
        result = ScanResult(bucket=self.config.bucket)
        for obj in objects:
            result.scanned_count += 1
            result.scanned_bytes += obj.size_bytes
            result.scanned_by_class[obj.storage_class] = (
                result.scanned_by_class.get(obj.storage_class, 0) + obj.size_bytes
            )
            rule_name = self.match(obj)
            if rule_name:
                result.candidates.append(Candidate(obj=obj, rule_name=rule_name))
        return result
