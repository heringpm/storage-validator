"""Top-level orchestration: resolve a backend, run discovery/health/perf, and
assemble a `Report`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

from storage_validator.backends.base import get_backend
from storage_validator.config import Config
from storage_validator.models import CheckResult, PerfResult, Report, Topology


def run(
    cfg: Config,
    on_topology: Callable[[Topology], None] | None = None,
    on_health: Callable[[list[CheckResult]], None] | None = None,
    on_perf_result: Callable[[list[PerfResult]], None] | None = None,
) -> Report:
    """Run a full validation pass (discovery + health + optional perf).

    `on_topology`/`on_health` are called once discovery/health checks
    finish; `on_perf_result` is called once per OST/pool perf check as it
    completes, so a caller (e.g. the CLI) can stream results to the console
    instead of waiting for the entire run to finish.
    """
    # Importing the backends package registers all built-in backends
    # (lustre, and future others) with BACKEND_REGISTRY as a side effect.
    import storage_validator.backends.lustre  # noqa: F401

    backend_cls = get_backend(cfg.backend)
    backend = backend_cls(health_cfg=cfg.health)

    topology = backend.discover()
    if on_topology:
        on_topology(topology)

    health_results = backend.run_health_checks(topology)
    if on_health:
        on_health(health_results)

    if cfg.skip_perf:
        perf_results = []
    else:
        perf_results = backend.run_perf_checks(topology, cfg.perf, on_result=on_perf_result)

    return Report(
        fsname=topology.fsname,
        timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        topology=topology,
        health=health_results,
        perf=perf_results,
    )
