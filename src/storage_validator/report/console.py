"""Render a `Report` as rich console tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from storage_validator.models import CheckResult, PerfResult, Report, Topology

_STATUS_STYLE = {"PASS": "green", "WARN": "yellow", "FAIL": "bold red"}


def _styled_status(status: str) -> str:
    style = _STATUS_STYLE.get(status, "white")
    return f"[{style}]{status}[/{style}]"


def styled_overall_status(status: str) -> str:
    """Public wrapper around `_styled_status`, for callers (e.g. the CLI)
    that need to print just the overall status string with the same
    PASS/WARN/FAIL color coding used in the report tables.
    """
    return _styled_status(status)


def build_topology_table(topology: Topology) -> Table:
    table = Table(title=f"Topology: {topology.fsname}")
    table.add_column("Target")
    table.add_column("Kind")
    table.add_column("State")
    table.add_column("Use%")
    use_pct_by_uuid = {
        uuid: info.get("use_pct") for uuid, info in topology.df.items()
    }
    for target in [*topology.mdts, *topology.osts]:
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
    table.add_column("I/O")
    table.add_column("Kind")
    table.add_column("Value")
    table.add_column("Status")
    table.add_column("Message")
    for result in results:
        add_perf_row(table, result)
    return table


def render_report(report: Report, console: Console | None = None) -> None:
    """Print the full report (topology, health, perf, overall status)."""
    console = console or Console()
    console.print(build_topology_table(report.topology))
    console.print(build_health_table(report.health))
    if report.perf:
        console.print(build_perf_table(report.perf))
    overall = report.overall_status()
    console.print(f"Overall status: {_styled_status(overall)}")


def add_perf_row(table: Table, result: PerfResult) -> None:
    """Append one `PerfResult` as a row to a perf table built with
    `build_perf_table`/`Table()`, used to stream rows in as checks finish
    instead of only rendering the full table once every check is done.
    """
    table.add_row(
        result.target,
        result.io_mode,
        result.kind,
        f"{result.value:.2f} {result.unit}",
        _styled_status(result.status),
        result.message,
    )
