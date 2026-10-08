import json
from unittest.mock import patch

from click.testing import CliRunner

from storage_validator.cli import main
from storage_validator.models import Report, Topology


def make_report(status_results=None):
    from storage_validator.models import CheckResult

    return Report(
        fsname="scratch",
        timestamp="2026-10-08T00:00:00+00:00",
        topology=Topology(fsname="scratch"),
        health=status_results or [CheckResult(name="x", status="PASS", message="ok")],
        perf=[],
    )


def test_cli_exits_zero_on_pass():
    runner = CliRunner()
    with patch("storage_validator.cli.engine.run", return_value=make_report()):
        result = runner.invoke(main, ["--skip-perf", "--quiet"])
    assert result.exit_code == 0


def test_cli_exits_two_on_fail():
    from storage_validator.models import CheckResult

    runner = CliRunner()
    report = make_report([CheckResult(name="x", status="FAIL", message="bad")])
    with patch("storage_validator.cli.engine.run", return_value=report):
        result = runner.invoke(main, ["--skip-perf", "--quiet"])
    assert result.exit_code == 2


def test_cli_writes_json_report(tmp_path):
    out_path = tmp_path / "out.json"
    runner = CliRunner()
    with patch("storage_validator.cli.engine.run", return_value=make_report()):
        result = runner.invoke(main, ["--skip-perf", "--quiet", "--json", str(out_path)])
    assert result.exit_code == 0
    data = json.loads(out_path.read_text())
    assert data["fsname"] == "scratch"


def test_cli_prints_console_output_by_default():
    runner = CliRunner()
    with patch("storage_validator.cli.engine.run", return_value=make_report()):
        result = runner.invoke(main, ["--skip-perf"])
    assert "scratch" in result.output
