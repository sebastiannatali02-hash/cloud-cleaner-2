import csv
import io
from datetime import datetime, timedelta, timezone

from cloudcleaner.models import Candidate, ScanResult, StorageObject
from cloudcleaner.pricing import Savings
from cloudcleaner.report import csv_report, html_report

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def obj(key: str, age_days: int = 0, size: int = 1024, storage_class: str = "STANDARD"):
    return StorageObject(
        key=key,
        size_bytes=size,
        last_modified=NOW - timedelta(days=age_days),
        storage_class=storage_class,
    )


def make_result() -> ScanResult:
    result = ScanResult(
        bucket="acme-corp-data",
        scanned_count=3,
        scanned_bytes=3072,
        scanned_by_class={"STANDARD": 3072},
    )
    result.candidates = [
        Candidate(obj("logs/app<1>.log", age_days=400, size=1024), "old-logs"),
        Candidate(obj("tmp/cache.tmp", age_days=5, size=2048), "tmp-files"),
    ]
    return result


def empty_result() -> ScanResult:
    return ScanResult(bucket="lean-bucket", scanned_count=0, scanned_bytes=0)


SAVINGS = Savings(current_monthly=12.3456, after_monthly=4.2, currency="USD")


class TestHtmlReport:
    def test_is_html_document(self):
        html = html_report(make_result(), SAVINGS, retention_days=30)
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert "<html" in html and "</html>" in html
        assert "<style>" in html  # inline CSS, self-contained
        assert "http://" not in html and "https://" not in html  # no external assets

    def test_contains_key_figures(self):
        html = html_report(make_result(), SAVINGS, retention_days=30)
        # headline monthly + yearly savings
        assert f"{SAVINGS.monthly:,.2f}" in html
        assert f"{SAVINGS.yearly:,.2f}" in html
        assert "acme-corp-data" in html
        assert "old-logs" in html
        assert "tmp-files" in html
        assert "30 days" in html

    def test_escapes_dynamic_strings(self):
        html = html_report(make_result(), SAVINGS, retention_days=30)
        # the raw key contained < and >, which must be escaped
        assert "logs/app<1>.log" not in html
        assert "logs/app&lt;1&gt;.log" in html

    def test_empty_bucket(self):
        html = html_report(empty_result(), SAVINGS, retention_days=30)
        assert "No objects matched" in html
        assert html.lstrip().startswith("<!DOCTYPE html>")


class TestCsvReport:
    def test_parses_back_with_reader(self):
        text = csv_report(make_result(), SAVINGS)
        rows = list(csv.reader(io.StringIO(text)))

        assert rows[0] == ["key", "size_bytes", "last_modified", "storage_class", "rule"]
        expected_ts = (NOW - timedelta(days=400)).isoformat()
        assert rows[1] == ["logs/app<1>.log", "1024", expected_ts, "STANDARD", "old-logs"]
        assert rows[2][0] == "tmp/cache.tmp"
        assert rows[2][4] == "tmp-files"

    def test_summary_rows_present(self):
        text = csv_report(make_result(), SAVINGS)
        rows = list(csv.reader(io.StringIO(text)))
        flat = {r[0]: r[1] for r in rows if len(r) == 2}
        assert flat["bucket"] == "acme-corp-data"
        assert flat["candidate_objects"] == "2"
        assert flat["currency"] == "USD"
        assert float(flat["monthly_savings"]) == round(SAVINGS.monthly, 4)

    def test_empty_bucket_has_header_and_summary(self):
        text = csv_report(empty_result(), SAVINGS)
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == ["key", "size_bytes", "last_modified", "storage_class", "rule"]
        flat = {r[0]: r[1] for r in rows if len(r) == 2}
        assert flat["candidate_objects"] == "0"
