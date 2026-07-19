import pytest

from cloudcleaner.cli import main

CONFIG = """
provider: memory
bucket: empty-bucket
rules:
  - name: logs
    keywords: [log]
    older_than: 90d
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(CONFIG)
    return str(path)


class TestCli:
    def test_scan_empty_bucket(self, config_file, capsys):
        assert main(["scan", "--config", config_file]) == 0
        out = capsys.readouterr().out
        assert "No objects matched" in out

    def test_scan_json(self, config_file, capsys):
        assert main(["scan", "--config", config_file, "--json"]) == 0
        out = capsys.readouterr().out
        assert '"bucket": "empty-bucket"' in out

    def test_quarantine_dry_run_by_default(self, config_file, capsys):
        assert main(["quarantine", "--config", config_file]) == 0
        out = capsys.readouterr().out
        assert "nothing to quarantine" in out

    def test_invalid_config_exit_code(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("bucket: b\nrules:\n  - name: empty\n")
        assert main(["scan", "--config", str(bad)]) == 2
        assert "config error" in capsys.readouterr().err

    def test_missing_config_file(self, capsys):
        assert main(["scan", "--config", "/does/not/exist.yaml"]) == 1

    def test_demo_runs(self, capsys):
        assert main(["demo"]) == 0
        out = capsys.readouterr().out
        assert "Estimated savings" in out
