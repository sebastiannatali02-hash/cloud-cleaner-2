"""Rule engine: decides which objects are cleanup candidates.

An object becomes a candidate when at least one rule matches it
(conditions inside a rule are ANDed). Exclusion patterns always win,
and anything under the quarantine prefix is never re-selected.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone

from .config import Config, Rule, parse_cutoff, parse_size
from .models import Candidate, ScanResult, StorageObject


class RuleEngine:
    def __init__(self, config: Config, now: datetime | None = None):
        self.config = config
        self.now = now or datetime.now(timezone.utc)
        self._compiled: list[tuple[Rule, re.Pattern | None, datetime | None, int | None]] = [
            (
                rule,
                re.compile(rule.match_regex) if rule.match_regex else None,
                parse_cutoff(rule.older_than, self.now) if rule.older_than else None,
                parse_size(rule.min_size) if rule.min_size is not None else None,
            )
            for rule in config.rules
        ]

    def is_excluded(self, key: str) -> bool:
        if key.startswith(self.config.quarantine.prefix):
            return True
        for pattern in self.config.exclude:
            # A bare prefix pattern ("legal-hold/") protects the whole subtree;
            # anything else is treated as a glob against the full key.
            if pattern.endswith("/") and key.startswith(pattern):
                return True
            if fnmatch.fnmatch(key, pattern):
                return True
        return False

    def match(self, obj: StorageObject) -> str | None:
        """Return the name of the first matching rule, or None."""
        if self.is_excluded(obj.key):
            return None
        key_lower = obj.key.lower()
        for rule, regex, cutoff, min_size in self._compiled:
            if rule.keywords and not any(k.lower() in key_lower for k in rule.keywords):
                continue
            if regex and not regex.search(obj.key):
                continue
            if rule.prefixes and not any(obj.key.startswith(p) for p in rule.prefixes):
                continue
            if rule.suffixes and not any(key_lower.endswith(s.lower()) for s in rule.suffixes):
                continue
            if cutoff and obj.last_modified >= cutoff:
                continue
            if min_size is not None and obj.size_bytes < min_size:
                continue
            if rule.storage_classes and obj.storage_class not in rule.storage_classes:
                continue
            return rule.name
        return None

    def scan(self, objects) -> ScanResult:
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
