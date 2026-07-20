"""Tests for boolean composition (match trees) and keep-newest-per-group."""

import pytest

from cloudcleaner.config import Config, ConfigError, QuarantineSettings, Rule
from cloudcleaner.rules import RuleEngine

from conftest import GIB, NOW, obj


def _engine(rules, exclude=None):
    config = Config(
        provider="memory",
        bucket="test-bucket",
        rules=rules,
        exclude=exclude or [],
        quarantine=QuarantineSettings(retention_days=30),
    )
    return RuleEngine(config, now=NOW)


class TestBackwardCompat:
    def test_flat_conditions_still_and(self):
        e = _engine([Rule(name="old-logs", keywords=["log"], older_than="90d")])
        assert e.match(obj("app/server.log", age_days=120)) == "old-logs"
        assert e.match(obj("app/server.log", age_days=10)) is None
        assert e.match(obj("app/server.txt", age_days=120)) is None


class TestAnyOf:
    def test_any_of_ors_leaves(self):
        rule = Rule(
            name="junk",
            match={"any_of": [{"suffixes": [".tmp"]}, {"suffixes": [".bak"]}]},
        )
        e = _engine([rule])
        assert e.match(obj("a/x.tmp")) == "junk"
        assert e.match(obj("a/x.bak")) == "junk"
        assert e.match(obj("a/x.keep")) is None


class TestAllOf:
    def test_all_of_ands_leaves(self):
        rule = Rule(
            name="big-old-logs",
            match={
                "all_of": [
                    {"keywords": ["log"]},
                    {"older_than": "90d"},
                    {"min_size": "1GB"},
                ]
            },
        )
        e = _engine([rule])
        assert e.match(obj("app/a.log", age_days=120, size=2 * GIB)) == "big-old-logs"
        assert e.match(obj("app/a.log", age_days=120, size=100)) is None  # too small
        assert e.match(obj("app/a.log", age_days=1, size=2 * GIB)) is None  # too fresh


class TestNoneOf:
    def test_none_of_negates(self):
        rule = Rule(
            name="tmp-not-legal",
            match={
                "all_of": [{"suffixes": [".tmp"]}],
                "none_of": [{"prefixes": ["legal/"]}],
            },
        )
        e = _engine([rule])
        assert e.match(obj("build/x.tmp")) == "tmp-not-legal"
        assert e.match(obj("legal/x.tmp")) is None

    def test_not_alias(self):
        rule = Rule(
            name="not-keep",
            match={"all_of": [{"suffixes": [".dat"]}], "not": {"keywords": ["keep"]}},
        )
        e = _engine([rule])
        assert e.match(obj("a/x.dat")) == "not-keep"
        assert e.match(obj("a/keep-x.dat")) is None


class TestNested:
    def test_nested_tree(self):
        # (log OR tmp) AND NOT under legal/
        rule = Rule(
            name="nested",
            match={
                "all_of": [
                    {"any_of": [{"keywords": ["log"]}, {"suffixes": [".tmp"]}]},
                    {"none_of": [{"prefixes": ["legal/"]}]},
                ]
            },
        )
        e = _engine([rule])
        assert e.match(obj("app/server.log")) == "nested"
        assert e.match(obj("app/cache.tmp")) == "nested"
        assert e.match(obj("legal/server.log")) is None
        assert e.match(obj("app/readme.md")) is None

    def test_flat_conditions_and_match_tree_both_apply(self):
        # flat prefix condition AND match tree are ANDed together
        rule = Rule(
            name="scoped",
            prefixes=["data/"],
            match={"any_of": [{"suffixes": [".tmp"]}, {"suffixes": [".log"]}]},
        )
        e = _engine([rule])
        assert e.match(obj("data/x.tmp")) == "scoped"
        assert e.match(obj("other/x.tmp")) is None  # fails flat prefix
        assert e.match(obj("data/x.keep")) is None  # fails match tree


class TestKeepNewest:
    def _backups(self):
        # 5 full dumps of increasing age
        return [
            obj("backups/db/full-2026-07-14.dump", age_days=1),
            obj("backups/db/full-2026-07-10.dump", age_days=5),
            obj("backups/db/full-2026-07-05.dump", age_days=10),
            obj("backups/db/full-2026-06-15.dump", age_days=30),
            obj("backups/db/full-2026-05-15.dump", age_days=61),
        ]

    def test_keeps_n_newest_flags_rest(self):
        rule = Rule(
            name="keep-3-full",
            prefixes=["backups/db/full-"],
            keep_newest=3,
            group_by="prefix:2",
        )
        e = _engine([rule])
        result = e.scan(self._backups())
        selected = {c.obj.key for c in result.candidates}
        # The 2 oldest are selected; the 3 newest protected.
        assert selected == {
            "backups/db/full-2026-06-15.dump",
            "backups/db/full-2026-05-15.dump",
        }
        assert all(c.rule_name == "keep-3-full" for c in result.candidates)

    def test_groups_are_independent(self):
        objs = [
            obj("backups/a/full-1.dump", age_days=1),
            obj("backups/a/full-2.dump", age_days=2),
            obj("backups/a/full-3.dump", age_days=3),
            obj("backups/b/full-1.dump", age_days=1),
            obj("backups/b/full-2.dump", age_days=2),
        ]
        rule = Rule(
            name="keep-1",
            prefixes=["backups/"],
            keep_newest=1,
            group_by="prefix:2",  # group by backups/<name>
        )
        e = _engine([rule])
        result = e.scan(objs)
        selected = {c.obj.key for c in result.candidates}
        # per group keep the single newest, flag the rest
        assert selected == {
            "backups/a/full-2.dump",
            "backups/a/full-3.dump",
            "backups/b/full-2.dump",
        }

    def test_group_by_regex_capture(self):
        objs = [
            obj("logs/app1/2026-07-01.log", age_days=1),
            obj("logs/app1/2026-06-01.log", age_days=31),
            obj("logs/app2/2026-07-01.log", age_days=1),
        ]
        rule = Rule(
            name="keep-1-per-app",
            keep_newest=1,
            group_by=r"logs/(app\d+)/",
        )
        e = _engine([rule])
        result = e.scan(objs)
        selected = {c.obj.key for c in result.candidates}
        assert selected == {"logs/app1/2026-06-01.log"}

    def test_excluded_never_selected(self):
        objs = [
            obj("backups/db/full-1.dump", age_days=1),
            obj("backups/db/full-2.dump", age_days=2),
            obj("backups/db/full-3.dump", age_days=3),
        ]
        rule = Rule(
            name="keep-1",
            prefixes=["backups/"],
            keep_newest=1,
            group_by="prefix:2",
        )
        e = _engine([rule], exclude=["backups/db/full-3.dump"])
        result = e.scan(objs)
        selected = {c.obj.key for c in result.candidates}
        # full-3 is excluded so never a candidate; full-1 is newest kept;
        # only full-2 is flagged.
        assert selected == {"backups/db/full-2.dump"}

    def test_keep_newest_zero_flags_all_in_group(self):
        objs = [
            obj("tmp/a", age_days=1),
            obj("tmp/b", age_days=2),
        ]
        rule = Rule(name="drop-all", prefixes=["tmp/"], keep_newest=0, group_by="prefix:1")
        e = _engine([rule])
        result = e.scan(objs)
        assert {c.obj.key for c in result.candidates} == {"tmp/a", "tmp/b"}


class TestValidation:
    def test_keep_newest_requires_group_by(self):
        with pytest.raises(ConfigError, match="requires group_by"):
            Rule(name="bad", prefixes=["x/"], keep_newest=3)

    def test_group_by_without_keep_newest(self):
        with pytest.raises(ConfigError, match="only valid together with keep_newest"):
            Rule(name="bad", prefixes=["x/"], group_by="prefix:2")

    def test_match_leaf_cannot_mix_with_group_op(self):
        with pytest.raises(ConfigError, match="either a condition leaf or a logical group"):
            Rule(name="bad", match={"any_of": [{"suffixes": [".tmp"]}], "keywords": ["x"]})

    def test_match_unknown_key(self):
        with pytest.raises(ConfigError, match="unknown keys in match node"):
            Rule(name="bad", match={"whatever": 1})

    def test_empty_match_node(self):
        with pytest.raises(ConfigError, match="empty match node"):
            Rule(name="bad", match={"any_of": [{}]})

    def test_invalid_group_by_prefix(self):
        with pytest.raises(ConfigError, match="positive integer"):
            Rule(name="bad", keep_newest=1, group_by="prefix:0")

    def test_rule_with_only_match_is_valid(self):
        # a match tree alone is enough conditions
        Rule(name="ok", match={"any_of": [{"suffixes": [".tmp"]}]})
