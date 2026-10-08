"""Render a `Report` as rich console tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from storage_validator.models import CheckResult, PerfResult, Report

_STATUS_STYLE = {"PASS": "green", "WARN": "yellow", "FAIL": "bold red"}


def _styled_status(status: str) -> str:
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{status}[/{style}]"


def build_topology_table(report: Report) -> Table:
    table = Table(title=f"Topology: {report.fsname}")
    table.add_column("Target")
    table.add_column("Kind")
    table.add_column("State")
    table.add_column("Use%")
    use_pct_by_uuid = {
        uuid: info.get("use_pct") for uuid, info in report.topology.df.items()
    }
    for target in [*report.topology.mdts, *report.topology.osts]:
        use_pct = use_pct_by_uuid.get(target.uuid)
        table.add_row(
            target.name,
            target.kind.upper(),
            target.state or "?",
            f"{use_pct}%" if use_pct is not None else "?",
        )
    return table


def build_health_table(results: list[CheckResult]) -> Table:
    table = Table(title="Health Checks")
    table.add_column("Check")
    table.add_column("Target")
    table.add_column("Status")
    table.add_column("Message")
    for result in results:
        table.add_row(
            result.name,
            result.target or "-",
            _styled_status(result.status),
            result.message,
        )
    return table


def build_perf_table(results: list[PerfResult]) -> Table:
    table = Table(title="Performance Checks")
    table.add_column("Target")
    table.add_column("Kind")
    table.add_column("Value")
    table.add_column("Status")
    table.add_column("Message")
    for result in results:
        table.add_row(
            result.target,
            result.kind,
            f"{result.value:.2f} {result.unit}",
            _styled_status(result.status),
            result.message,
        )
    return table


def render_report(report: Report, console: Console | None = None) -> None:
    """Print the full report (topology, health, perf, overall status)."""
    console = console or Console()
    console.print(build_topology_table(report))
    console.print(build_health_table(report.health))
    if report.perf:
        console.print(build_perf_table(report.perf))
    overall = report.overall_status()
    console.print(f"Overall status: {_styled_status(overall)}")
