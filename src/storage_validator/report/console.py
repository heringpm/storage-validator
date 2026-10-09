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
    table.add_column("I/O")
    table.add_column("Throughput")
    table.add_column("Tput Status")
    table.add_column("Latency")
    table.add_column("Lat Status")
    table.add_column("Message")


def _add_perf_row(table: Table, throughput: PerfResult | None, latency: PerfResult | None) -> None:
    """Append one combined row for a throughput+latency pair (same target
    and io_mode), with throughput and latency each in their own columns.
    Either `throughput` or `latency` may be None if a pair couldn't be
    matched, in which case that column shows "-".
    """
    anchor = throughput or latency
    table.add_row(
        anchor.target,
        anchor.io_mode,
        f"{throughput.value:.2f} {throughput.unit}" if throughput else "-",
        _styled_status(throughput.status) if throughput else "-",
        f"{latency.value:.2f} {latency.unit}" if latency else "-",
        _styled_status(latency.status) if latency else "-",
        " / ".join(
            escape(r.message) for r in (throughput, latency) if r and r.message
        ),
    )


def build_perf_table(results: list[PerfResult]) -> Table:
    """Build a perf table with one row per (target, io_mode) pair, showing
    throughput and latency side by side in separate columns instead of as
    separate rows.
    """
    table = Table(title="Performance Checks")
    _add_perf_columns(table)
    pending: dict[tuple, PerfResult] = {}
    for result in results:
        key = (result.target, result.io_mode)
        other = pending.pop(key, None)
        if other is None:
            pending[key] = result
            continue
        tp, lat = (result, other) if result.kind == "throughput" else (other, result)
        _add_perf_row(table, tp, lat)
    for result in pending.values():
        if result.kind == "throughput":
            _add_perf_row(table, result, None)
        else:
            _add_perf_row(table, None, result)
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
    """Append two rows (one for write, one for read) to a perf table built
    with `build_perf_table`/`_add_perf_columns`, from a single check's
    `(write_throughput, write_latency, read_throughput, read_latency)`
    result tuple. Used to stream rows in as checks finish instead of only
    rendering the full table once every check is done.
    """
    write_tp, write_lat, read_tp, read_lat = quad
    _add_perf_row(table, write_tp, write_lat)
    _add_perf_row(table, read_tp, read_lat)
