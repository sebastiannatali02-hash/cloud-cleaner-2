from cloudcleaner.rules import RuleEngine

from conftest import GIB, NOW, obj


def engine(config):
    return RuleEngine(config, now=NOW)


class TestMatching:
    def test_keyword_and_age_must_both_hold(self, config):
        e = engine(config)
        assert e.match(obj("app/server.log", age_days=120)) == "old-logs"
        assert e.match(obj("app/server.log", age_days=10)) is None  # too recent
        assert e.match(obj("app/server.txt", age_days=120)) is None  # no keyword

    def test_keyword_is_case_insensitive(self, config):
        assert engine(config).match(obj("app/SERVER.LOG", age_days=120)) == "old-logs"

    def test_suffix_rule(self, config):
        e = engine(config)
        assert e.match(obj("build/cache.tmp")) == "tmp-files"
        assert e.match(obj("build/cache.tmpx")) is None

    def test_prescription_period_on_prefix(self, config):
        e = engine(config)
        assert e.match(obj("invoices/2010/inv-1.pdf", age_days=11 * 365)) == "invoices-prescribed"
        assert e.match(obj("invoices/2020/inv-2.pdf", age_days=5 * 365)) is None
        assert e.match(obj("archive/2010/inv-1.pdf", age_days=11 * 365)) is None

    def test_min_size(self, config):
        e = engine(config)
        assert e.match(obj("backups/full.dump", age_days=400, size=2 * GIB)) == "big-old"
        assert e.match(obj("backups/full.dump", age_days=400, size=GIB - 1)) is None

    def test_first_matching_rule_wins(self, config):
        # Matches both old-logs and big-old; rule order decides attribution.
        assert engine(config).match(obj("app/huge.log", age_days=400, size=2 * GIB)) == "old-logs"


class TestExclusions:
    def test_directory_exclusion_beats_rules(self, config):
        assert engine(config).match(obj("legal-hold/evidence.log", age_days=999)) is None

    def test_glob_exclusion(self, config):
        assert engine(config).match(obj("data/users.db", age_days=999, size=2 * GIB)) is None

    def test_quarantine_area_never_rescanned(self, config):
        key = f"{config.quarantine.prefix}20260101T000000Z/objects/app/server.log"
        assert engine(config).match(obj(key, age_days=999)) is None


class TestScan:
    def test_scan_aggregates(self, config):
        objects = [
            obj("app/server.log", age_days=120, size=100),
            obj("app/fresh.log", age_days=1, size=100),
            obj("build/cache.tmp", size=50),
            obj("legal-hold/evidence.log", age_days=999, size=100),
        ]
        result = engine(config).scan(objects)
        assert result.scanned_count == 4
        assert result.scanned_bytes == 350
        assert result.candidate_count == 2
        assert result.candidate_bytes == 150
        assert {c.rule_name for c in result.candidates} == {"old-logs", "tmp-files"}
        assert result.scanned_by_class == {"STANDARD": 350}
        assert result.candidates_by_class == {"STANDARD": 150}
