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
    report = make_report()

    def fake_run(cfg, on_topology=None, on_health=None, on_perf_result=None):
        if on_topology:
            on_topology(report.topology)
        if on_health:
            on_health(report.health)
        return report

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(main, ["--skip-perf"])
    assert "scratch" in result.output


def test_cli_parses_pool_threshold_option():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(
            main,
            [
                "--skip-perf",
                "--quiet",
                "--pool-threshold",
                "flash:800:400:2:5",
                "--pool-threshold",
                "archive:100:20:20:80",
            ],
        )
    assert result.exit_code == 0
    thresholds = captured["cfg"].perf.pool_thresholds
    assert set(thresholds) == {"flash", "archive"}
    assert thresholds["flash"].warn_mbps == 800.0
    assert thresholds["flash"].fail_mbps == 400.0
    assert thresholds["archive"].fail_ms == 80.0


def test_cli_parses_elbencho_path_option():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(
            main,
            ["--skip-perf", "--quiet", "--elbencho-path", "/opt/elbencho/bin/elbencho"],
        )
    assert result.exit_code == 0
    assert captured["cfg"].perf.elbencho_path == "/opt/elbencho/bin/elbencho"


def test_cli_parses_perf_threads_option():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(
            main, ["--skip-perf", "--quiet", "--perf-threads", "8"]
        )
    assert result.exit_code == 0
    assert captured["cfg"].perf.perf_threads == 8


def test_cli_defaults_perf_threads_to_none():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(main, ["--skip-perf", "--quiet"])
    assert result.exit_code == 0
    assert captured["cfg"].perf.perf_threads is None


def test_cli_parses_ost_and_pool_filter_options():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(
            main,
            [
                "--skip-perf",
                "--quiet",
                "--ost",
                "scratch-OST0000",
                "--ost",
                "scratch-OST0001",
                "--pool",
                "flash",
            ],
        )
    assert result.exit_code == 0
    assert captured["cfg"].perf.ost_names == {"scratch-OST0000", "scratch-OST0001"}
    assert captured["cfg"].perf.pool_names == {"flash"}


def test_cli_defaults_ost_and_pool_filters_to_none():
    runner = CliRunner()
    captured = {}

    def fake_run(cfg):
        captured["cfg"] = cfg
        return make_report()

    with patch("storage_validator.cli.engine.run", side_effect=fake_run):
        result = runner.invoke(main, ["--skip-perf", "--quiet"])
    assert result.exit_code == 0
    assert captured["cfg"].perf.ost_names is None
    assert captured["cfg"].perf.pool_names is None


def test_cli_rejects_malformed_pool_threshold():
    runner = CliRunner()
    with patch("storage_validator.cli.engine.run", return_value=make_report()):
        result = runner.invoke(
            main, ["--skip-perf", "--quiet", "--pool-threshold", "flash:bad"]
        )
    assert result.exit_code != 0
