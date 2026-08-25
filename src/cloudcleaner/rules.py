"""Rule engine: decides which objects are cleanup candidates.

An object becomes a candidate when at least one rule matches it
(conditions inside a rule are ANDed). Exclusion patterns always win,
and anything under the quarantine prefix is never re-selected.

Each rule condition compiles to an independent predicate via a builder
in ``_CONDITION_BUILDERS``; supporting a new condition means adding one
builder there (open/closed) rather than editing the matching loop.

Two richer selection modes layer on top of the flat ANDed conditions:

* A ``match`` tree of nested ``any_of`` / ``all_of`` / ``none_of`` / ``not``
  groups whose leaves are the same condition dicts. Compiled by
  :func:`compile_match_node` and ANDed with the flat conditions.
* ``keep_newest`` + ``group_by``: a set-oriented mode where, per group, the N
  newest objects are protected and the rest selected. Applied as a post-pass in
  :meth:`RuleEngine.scan` since it needs the whole scanned set at once.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from cloudcleaner.config import (
    Config,
    Rule,
    RULE_CONDITION_FIELDS,
    parse_cutoff,
    parse_size,
)
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
    """All active conditions of the rule as predicates (ANDed on match).

    Includes both the flat condition fields and, when present, the nested
    ``match`` tree (compiled to a single predicate). All are ANDed together.
    """
    predicates = [p for builder in _CONDITION_BUILDERS if (p := builder(rule, now)) is not None]
    if rule.match is not None:
        predicates.append(compile_match_node(rule.match, now))
    return predicates


def _compile_leaf(conditions: dict, now: datetime) -> Predicate:
    """Compile a bag of condition fields into one ANDed predicate.

    Reuses the same builders as flat rules by wrapping the dict in a throwaway
    :class:`Rule`, so leaves and top-level flat conditions behave identically.
    """
    leaf_rule = Rule(name="<match-leaf>", **{
        k: v for k, v in conditions.items() if k in RULE_CONDITION_FIELDS
    })
    predicates = [p for builder in _CONDITION_BUILDERS if (p := builder(leaf_rule, now)) is not None]
    return lambda obj: all(p(obj) for p in predicates)


def compile_match_node(node: dict, now: datetime) -> Predicate:
    """Compile a validated ``match`` tree into a single predicate.

    A node is either a condition leaf (fields ANDed) or a logical group with
    ``any_of`` (OR), ``all_of`` (AND), ``none_of``/``not`` (negation). Multiple
    operator keys in one group node are ANDed together.
    """
    group_keys = set(node) & {"any_of", "all_of", "none_of", "not"}
    if not group_keys:
        return _compile_leaf(node, now)

    parts: list[Predicate] = []
    for op in group_keys:
        children = node[op]
        if op == "not":
            child_list = children if isinstance(children, list) else [children]
            sub = [compile_match_node(c, now) for c in child_list]
            parts.append(lambda obj, sub=sub: not any(p(obj) for p in sub))
        elif op == "none_of":
            sub = [compile_match_node(c, now) for c in children]
            parts.append(lambda obj, sub=sub: not any(p(obj) for p in sub))
        elif op == "any_of":
            sub = [compile_match_node(c, now) for c in children]
            parts.append(lambda obj, sub=sub: any(p(obj) for p in sub))
        else:  # all_of
            sub = [compile_match_node(c, now) for c in children]
            parts.append(lambda obj, sub=sub: all(p(obj) for p in sub))
    return lambda obj: all(p(obj) for p in parts)


def _group_key_fn(spec: str) -> Callable[[StorageObject], str | None]:
    """Build the grouping function for a ``group_by`` spec.

    * ``prefix:<N>`` groups by the first N ``/``-separated path segments.
    * anything else is a regex; objects group by the concatenation of capture
      groups (or the whole match if there are none). A non-matching object
      returns ``None`` and is left untouched by the keep-newest pass.
    """
    if spec.startswith("prefix:"):
        depth = int(spec[len("prefix:"):])

        def by_prefix(obj: StorageObject) -> str | None:
            parts = obj.key.split("/")
            if len(parts) < depth:
                return None
            return "/".join(parts[:depth])

        return by_prefix

    pattern = re.compile(spec)

    def by_regex(obj: StorageObject) -> str | None:
        m = pattern.search(obj.key)
        if not m:
            return None
        return "".join(m.groups()) if m.groups() else m.group(0)

    return by_regex


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
        # Per-object rules are matched one object at a time by ``match``.
        # keep_newest rules are set-oriented and applied as a post-pass in
        # ``scan`` since they need every object in a group at once.
        self._compiled: list[tuple[str, list[Predicate]]] = [
            (rule.name, compile_rule(rule, self.now))
            for rule in config.rules
            if rule.keep_newest is None
        ]
        self._keep_newest_rules: list[Rule] = [
            rule for rule in config.rules if rule.keep_newest is not None
        ]

    def is_excluded(self, key: str) -> bool:
        return self.exclusions.is_excluded(key)

    def match(self, obj: StorageObject) -> str | None:
        """Return the name of the first matching per-object rule, or None.

        keep_newest rules are intentionally not consulted here: they can only be
        resolved against the whole scanned set (see :meth:`scan`).
        """
        if self.exclusions.is_excluded(obj.key):
            return None
        for rule_name, predicates in self._compiled:
            if all(predicate(obj) for predicate in predicates):
                return rule_name
        return None

    def scan(self, objects: Iterable[StorageObject]) -> ScanResult:
        result = ScanResult(bucket=self.config.bucket)
        materialised: list[StorageObject] = []
        already_selected: set[str] = set()
        for obj in objects:
            materialised.append(obj)
            result.scanned_count += 1
            result.scanned_bytes += obj.size_bytes
            result.scanned_by_class[obj.storage_class] = (
                result.scanned_by_class.get(obj.storage_class, 0) + obj.size_bytes
            )
            rule_name = self.match(obj)
            if rule_name:
                result.candidates.append(Candidate(obj=obj, rule_name=rule_name))
                already_selected.add(obj.key)

        self._apply_keep_newest(materialised, result, already_selected)
        return result

    def _apply_keep_newest(
        self,
        objects: list[StorageObject],
        result: ScanResult,
        already_selected: set[str],
    ) -> None:
        """Post-pass: per group, protect the N newest and select the rest.

        Excluded objects and objects already selected by a per-object rule are
        skipped. Objects a rule's own conditions/``match`` tree reject don't even
        enter the rule's groups.
        """
        for rule in self._keep_newest_rules:
            scope = compile_rule(rule, self.now)  # flat conditions + match tree
            group_of = _group_key_fn(rule.group_by)

            groups: dict[str, list[StorageObject]] = {}
            for obj in objects:
                if self.exclusions.is_excluded(obj.key):
                    continue
                if not all(p(obj) for p in scope):
                    continue
                key = group_of(obj)
                if key is None:
                    continue
                groups.setdefault(key, []).append(obj)

            for members in groups.values():
                # Newest first; keep the first ``keep_newest``, select the rest.
                # Tie-break on key so ordering is deterministic.
                members.sort(key=lambda o: (o.last_modified, o.key), reverse=True)
                for obj in members[rule.keep_newest:]:
                    if obj.key in already_selected:
                        continue
                    result.candidates.append(Candidate(obj=obj, rule_name=rule.name))
                    already_selected.add(obj.key)
