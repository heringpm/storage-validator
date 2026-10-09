"""`storage-validator` CLI entrypoint."""

from __future__ import annotations

import sys

import click
from rich.console import Console
from rich.live import Live

from storage_validator import engine
from storage_validator.config import Config, HealthConfig, PerfConfig, PerfThresholds
from storage_validator.report import console as console_report
from storage_validator.report import json_report

_EXIT_CODE = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _parse_pool_threshold(raw: str) -> tuple[str, PerfThresholds]:
    """Parse `--pool-threshold` values of the form
    `name:warn_mbps:fail_mbps:warn_ms:fail_ms`.
    """
    parts = raw.split(":")
    if len(parts) != 5:
        raise click.BadParameter(
            f"{raw!r}: expected format name:warn_mbps:fail_mbps:warn_ms:fail_ms"
        )
    name, warn_mbps, fail_mbps, warn_ms, fail_ms = parts
    try:
        thresholds = PerfThresholds(
            warn_mbps=float(warn_mbps),
            fail_mbps=float(fail_mbps),
            warn_ms=float(warn_ms),
            fail_ms=float(fail_ms),
        )
    except ValueError as exc:
        raise click.BadParameter(f"{raw!r}: {exc}") from exc
    return name, thresholds


@click.command()
@click.option("--backend", default="lustre", show_default=True, help="Storage backend to validate.")
@click.option("--mount-path", default=None, help="Client mount point to use for perf checks (autodetected if omitted).")
@click.option("--size", "perf_size", default="10g", show_default=True, help="Size of each read/write elbencho test's dataset (e.g. 1g, 500m, 10g).")
@click.option("--block-size", default="1m", show_default=True, help="elbencho I/O block size (e.g. 1m, 4k).")
@click.option("--timelimit", "perf_runtime", default=60, show_default=True, type=int, help="elbencho test runtime (s) per read/write run.")
@click.option("--timeout", default=90.0, show_default=True, help="Timeout (s) for each perf/health subprocess call.")
@click.option("--warn-pct", default=80, show_default=True, help="OST/MDT usage %% that triggers a WARN.")
@click.option("--fail-pct", default=95, show_default=True, help="OST/MDT usage %% that triggers a FAIL.")
@click.option("--warn-mbps", default=200.0, show_default=True, help="Throughput (MB/s) below which to WARN.")
@click.option("--fail-mbps", default=50.0, show_default=True, help="Throughput (MB/s) below which to FAIL.")
@click.option("--warn-ms", default=10.0, show_default=True, help="Write latency (ms) above which to WARN.")
@click.option("--fail-ms", default=50.0, show_default=True, help="Write latency (ms) above which to FAIL.")
@click.option("--skip-perf", is_flag=True, default=False, help="Skip throughput/latency perf checks.")
@click.option(
    "--perf-only",
    is_flag=True,
    default=False,
    help="Run only the perf checks: skip health checks and the topology/health table output, printing just the perf table.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print every lfs/lctl/elbencho command that would be run instead of running it. No real discovery/health/perf data is produced.",
)
@click.option(
    "--pool-threshold",
    "pool_thresholds",
    multiple=True,
    metavar="NAME:WARN_MBPS:FAIL_MBPS:WARN_MS:FAIL_MS",
    help=(
        "Per-pool perf thresholds, overriding --warn-mbps/--fail-mbps/--warn-ms/"
        "--fail-ms for OSTs in that Lustre OST pool (e.g. for mixed drive types "
        "like flash vs. archive). Repeatable."
    ),
)
@click.option(
    "--elbencho-path",
    default="elbencho",
    show_default=True,
    help="Path to the elbencho binary used for perf checks, if not on PATH.",
)
@click.option(
    "--perf-threads",
    default=None,
    type=int,
    help=(
        "Total elbencho worker thread count for a single perf test run "
        "(per-OST or per-pool). Defaults to the host's total CPU thread "
        "count (from `lscpu`)."
    ),
)
@click.option(
    "--ost",
    "ost_names",
    multiple=True,
    metavar="NAME",
    help="Restrict per-OST perf checks to this OST name (e.g. scratch-OST0000). Repeatable. Per-pool checks are unaffected.",
)
@click.option(
    "--pool",
    "pool_names",
    multiple=True,
    metavar="NAME",
    help="Restrict perf checks (both per-OST and per-pool) to this OST pool name. Repeatable.",
)
@click.option(
    "--hosts",
    default=None,
    metavar="HOST1,HOST2,...",
    help=(
        "Comma-separated list of extra hosts (reachable via passwordless SSH) "
        "to run elbencho on in distributed mode alongside this client, for "
        "maxing out throughput beyond one client. An elbencho daemon is "
        "started on each host before testing and stopped afterwards."
    ),
)
@click.option(
    "--hosts-file",
    default=None,
    type=click.Path(dir_okay=False, exists=True),
    help="Path to a file with one hostname per line, added to --hosts.",
)
@click.option("--json", "json_path", default=None, type=click.Path(dir_okay=False), help="Write the JSON report to this path.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress the console table output.")
def main(
    backend: str,
    mount_path: str | None,
    perf_size: str,
    block_size: str,
    perf_runtime: int,
    timeout: float,
    warn_pct: int,
    fail_pct: int,
    warn_mbps: float,
    fail_mbps: float,
    warn_ms: float,
    fail_ms: float,
    skip_perf: bool,
    perf_only: bool,
    dry_run: bool,
    pool_thresholds: tuple[str, ...],
    elbencho_path: str,
    perf_threads: int | None,
    ost_names: tuple[str, ...],
    pool_names: tuple[str, ...],
    hosts: str | None,
    hosts_file: str | None,
    json_path: str | None,
    quiet: bool,
) -> None:
    """Validate a storage filesystem: discovery, health checks, perf checks."""
    parsed_pool_thresholds = dict(_parse_pool_threshold(raw) for raw in pool_thresholds)
    parsed_hosts = [h.strip() for h in (hosts or "").split(",") if h.strip()]
    if hosts_file:
        with open(hosts_file) as f:
            parsed_hosts.extend(line.strip() for line in f if line.strip())
    cfg = Config(
        backend=backend,
        skip_perf=skip_perf,
        skip_health=perf_only,
        dry_run=dry_run,
        health=HealthConfig(warn_pct=warn_pct, fail_pct=fail_pct),
        perf=PerfConfig(
            mount_path=mount_path,
            perf_size=perf_size,
            perf_block_size=block_size,
            perf_runtime=perf_runtime,
            timeout=timeout,
            warn_mbps=warn_mbps,
            fail_mbps=fail_mbps,
            warn_ms=warn_ms,
            fail_ms=fail_ms,
            pool_thresholds=parsed_pool_thresholds,
            elbencho_path=elbencho_path,
            perf_threads=perf_threads,
            ost_names=set(ost_names) or None,
            pool_names=set(pool_names) or None,
            hosts=parsed_hosts or None,
        ),
    )

    if quiet or dry_run:
        # In dry-run mode, the only output that matters is the
        # `[DRY RUN] ...` command lines printed by engine.run() itself --
        # skip the topology/health/perf tables and overall-status line
        # entirely, since the report they'd describe isn't real data.
        report = engine.run(cfg)
    else:
        console = Console()
        perf_table = None
        live: Live | None = None

        def on_topology(topology) -> None:
            if not perf_only:
                console.print(console_report.build_topology_table(topology))

        def on_health(health_results) -> None:
            if not perf_only:
                console.print(console_report.build_health_table(health_results))

        def on_perf_result(quad) -> None:
            nonlocal perf_table, live
            if perf_table is None:
                perf_table = console_report.build_perf_table([])
                live = Live(perf_table, console=console, refresh_per_second=4)
                live.start()
            console_report.add_perf_quad_rows(perf_table, quad)
            live.refresh()

        report = engine.run(
            cfg,
            on_topology=on_topology,
            on_health=on_health,
            on_perf_result=None if cfg.skip_perf else on_perf_result,
        )
        if live is not None:
            live.stop()
        overall = report.overall_status()
        console.print(f"Overall status: {console_report.styled_overall_status(overall)}")

    if json_path:
        json_report.write_report(report, json_path)

    sys.exit(_EXIT_CODE.get(report.overall_status(), 2))


if __name__ == "__main__":
    main()
