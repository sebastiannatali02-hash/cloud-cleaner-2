from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.config import ConfigError, Rule, load_config, parse_cutoff, parse_size

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestParseCutoff:
    def test_relative_days(self):
        assert parse_cutoff("90d", NOW) == NOW - timedelta(days=90)

    def test_relative_years_prescription_period(self):
        assert parse_cutoff("10y", NOW) == NOW - timedelta(days=3650)

    def test_relative_weeks_and_months(self):
        assert parse_cutoff("2w", NOW) == NOW - timedelta(days=14)
        assert parse_cutoff("6m", NOW) == NOW - timedelta(days=180)

    def test_iso_date(self):
        assert parse_cutoff("2016-01-01", NOW) == datetime(2016, 1, 1, tzinfo=timezone.utc)

    def test_invalid(self):
        with pytest.raises(ConfigError):
            parse_cutoff("dieci anni", NOW)


class TestParseSize:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1024, 1024),
            ("500", 500),
            ("500KB", 500 * 1024),
            ("1.5MB", int(1.5 * 1024**2)),
            ("1GB", 1024**3),
            ("2GiB", 2 * 1024**3),
        ],
    )
    def test_valid(self, value, expected):
        assert parse_size(value) == expected

    def test_invalid(self):
        with pytest.raises(ConfigError):
            parse_size("many bytes")


class TestRuleValidation:
    def test_rule_without_conditions_rejected(self):
        with pytest.raises(ConfigError, match="no conditions"):
            Rule(name="empty")

    def test_bad_regex_rejected(self):
        with pytest.raises(ConfigError, match="invalid regex"):
            Rule(name="bad", match_regex="([unclosed")


class TestLoadConfig:
    def test_full_roundtrip(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text(
            """
provider: s3
bucket: my-bucket
prefix: data/
rules:
  - name: logs
    keywords: [log]
    older_than: 90d
exclude:
  - legal-hold/
quarantine:
  prefix: q
  retention_days: 7
pricing:
  currency: EUR
  overrides:
    standard: 0.024
"""
        )
        config = load_config(path)
        assert config.bucket == "my-bucket"
        assert config.rules[0].name == "logs"
        assert config.quarantine.prefix == "q/"  # trailing slash added
        assert config.quarantine.retention_days == 7
        assert config.pricing.overrides == {"STANDARD": 0.024}
        assert config.pricing.currency == "EUR"

    def test_missing_bucket(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("rules:\n  - keywords: [x]\n")
        with pytest.raises(ConfigError, match="bucket"):
            load_config(path)

    def test_unknown_rule_key(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("bucket: b\nrules:\n  - keywords: [x]\n    keyward: [y]\n")
        with pytest.raises(ConfigError, match="unknown keys"):
            load_config(path)

    def test_empty_rules(self, tmp_path):
        path = tmp_path / "rules.yaml"
        path.write_text("bucket: b\nrules: []\n")
        with pytest.raises(ConfigError, match="non-empty"):
            load_config(path)
