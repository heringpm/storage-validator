"""`storage-validator` CLI entrypoint."""

from __future__ import annotations

import sys

import click

from storage_validator import engine
from storage_validator.config import Config, HealthConfig, PerfConfig
from storage_validator.report import console as console_report
from storage_validator.report import json_report

_EXIT_CODE = {"PASS": 0, "WARN": 1, "FAIL": 2}


@click.command()
@click.option("--backend", default="lustre", show_default=True, help="Storage backend to validate.")
@click.option("--mount-path", default=None, help="Client mount point to use for perf checks (autodetected if omitted).")
@click.option("--size-mb", default=256, show_default=True, help="Size (MiB) of the throughput test file.")
@click.option("--timeout", default=60.0, show_default=True, help="Timeout (s) for each perf/health subprocess call.")
@click.option("--warn-pct", default=80, show_default=True, help="OST/MDT usage %% that triggers a WARN.")
@click.option("--fail-pct", default=95, show_default=True, help="OST/MDT usage %% that triggers a FAIL.")
@click.option("--warn-mbps", default=200.0, show_default=True, help="Throughput (MB/s) below which to WARN.")
@click.option("--fail-mbps", default=50.0, show_default=True, help="Throughput (MB/s) below which to FAIL.")
@click.option("--warn-ms", default=10.0, show_default=True, help="Write latency (ms) above which to WARN.")
@click.option("--fail-ms", default=50.0, show_default=True, help="Write latency (ms) above which to FAIL.")
@click.option("--skip-perf", is_flag=True, default=False, help="Skip throughput/latency perf checks.")
@click.option("--json", "json_path", default=None, type=click.Path(dir_okay=False), help="Write the JSON report to this path.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress the console table output.")
def main(
    backend: str,
    mount_path: str | None,
    size_mb: int,
    timeout: float,
    warn_pct: int,
    fail_pct: int,
    warn_mbps: float,
    fail_mbps: float,
    warn_ms: float,
    fail_ms: float,
    skip_perf: bool,
    json_path: str | None,
    quiet: bool,
) -> None:
    """Validate a storage filesystem: discovery, health checks, perf checks."""
    cfg = Config(
        backend=backend,
        skip_perf=skip_perf,
        health=HealthConfig(warn_pct=warn_pct, fail_pct=fail_pct),
        perf=PerfConfig(
            mount_path=mount_path,
            size_mb=size_mb,
            timeout=timeout,
            warn_mbps=warn_mbps,
            fail_mbps=fail_mbps,
            warn_ms=warn_ms,
            fail_ms=fail_ms,
        ),
    )

    report = engine.run(cfg)

    if not quiet:
        console_report.render_report(report)

    if json_path:
        json_report.write_report(report, json_path)

    sys.exit(_EXIT_CODE.get(report.overall_status(), 2))


if __name__ == "__main__":
    main()
