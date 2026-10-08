import json

from rich.console import Console

from storage_validator.models import CheckResult, PerfResult, Report, Target, Topology
from storage_validator.report import console as console_report
from storage_validator.report import json_report


def make_report() -> Report:
    topology = Topology(
        fsname="scratch",
        osts=[Target(name="scratch-OST0000", kind="ost", uuid="u0", state="UP")],
        df={"u0": {"use_pct": 42}},
    )
    return Report(
        fsname="scratch",
        timestamp="2026-10-08T00:00:00+00:00",
        topology=topology,
        health=[CheckResult(name="target_state", status="PASS", message="ok", target="scratch-OST0000")],
        perf=[PerfResult(target="scratch-OST0000", kind="throughput", value=300.0, unit="MB/s", status="PASS")],
    )


def test_report_to_json_round_trips_and_includes_overall_status():
    report = make_report()
    data = json.loads(json_report.report_to_json(report))
    assert data["fsname"] == "scratch"
    assert data["overall_status"] == "PASS"
    assert data["health"][0]["status"] == "PASS"
    assert data["perf"][0]["value"] == 300.0


def test_write_report_creates_file(tmp_path):
    report = make_report()
    out = tmp_path / "report.json"
    json_report.write_report(report, str(out))
    data = json.loads(out.read_text())
    assert data["fsname"] == "scratch"


def test_render_report_prints_without_error():
    report = make_report()
    console = Console(record=True, width=120)
    console_report.render_report(report, console=console)
    text = console.export_text()
    assert "scratch-OST0000" in text
    assert "Overall status" in text


def test_overall_status_reflects_worst_result():
    report = make_report()
    report.health.append(CheckResult(name="x", status="FAIL", message="bad"))
    assert report.overall_status() == "FAIL"
