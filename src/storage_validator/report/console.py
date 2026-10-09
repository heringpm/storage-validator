"""Render a `Report` as rich console tables."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
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
            escape(result.message),
        )
    return table


def _add_perf_columns(table: Table) -> None:
    table.add_column("Target")
    table.add_column("Write Throughput")
    table.add_column("Write Latency")
    table.add_column("Read Throughput")
    table.add_column("Read Latency")
    table.add_column("Message")


def _value_cell(result: PerfResult | None) -> str:
    """One value+status cell, e.g. "[green]300.00 MB/s[/green]". Status is
    conveyed by color instead of a separate column, to keep one row per
    target (write+read together) from getting too wide.
    """
    if result is None:
        return "-"
    style = _STATUS_STYLE.get(result.status, "white")
    return f"[{style}]{result.value:.2f} {result.unit}[/{style}]"


def _add_perf_row(
    table: Table,
    write_tp: PerfResult | None,
    write_lat: PerfResult | None,
    read_tp: PerfResult | None,
    read_lat: PerfResult | None,
) -> None:
    """Append one row per target with write and read throughput/latency
    each in their own column, so a single OST's/pool's full result fits on
    one line. Any of the four results may be None if it couldn't be
    matched, in which case that column shows "-".
    """
    anchor = write_tp or write_lat or read_tp or read_lat
    messages = [
        f"{r.io_mode} {r.kind}: {escape(r.message)}"
        for r in (write_tp, write_lat, read_tp, read_lat)
        if r and r.status != "PASS" and r.message
    ]
    table.add_row(
        anchor.target,
        _value_cell(write_tp),
        _value_cell(write_lat),
        _value_cell(read_tp),
        _value_cell(read_lat),
        " / ".join(messages) if messages else "-",
    )


def build_perf_table(results: list[PerfResult]) -> Table:
    """Build a perf table with one row per target, showing write and read
    throughput/latency side by side in separate columns instead of as
    separate rows.
    """
    table = Table(title="Performance Checks")
    _add_perf_columns(table)
    order: list[str] = []
    by_target: dict[str, dict[str, dict[str, PerfResult]]] = {}
    for result in results:
        if result.target not in by_target:
            by_target[result.target] = {"write": {}, "read": {}}
            order.append(result.target)
        by_target[result.target][result.io_mode][result.kind] = result
    for target in order:
        modes = by_target[target]
        _add_perf_row(
            table,
            modes["write"].get("throughput"),
            modes["write"].get("latency"),
            modes["read"].get("throughput"),
            modes["read"].get("latency"),
        )
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


def add_perf_quad_rows(
    table: Table,
    quad: tuple[PerfResult, PerfResult, PerfResult, PerfResult],
) -> None:
    """Append one row to a perf table built with
    `build_perf_table`/`_add_perf_columns`, from a single check's
    `(write_throughput, write_latency, read_throughput, read_latency)`
    result tuple. Used to stream rows in as checks finish instead of only
    rendering the full table once every check is done.
    """
    write_tp, write_lat, read_tp, read_lat = quad
    _add_perf_row(table, write_tp, write_lat, read_tp, read_lat)
