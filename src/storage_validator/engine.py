"""Top-level orchestration: resolve a backend, run discovery/health/perf, and
assemble a `Report`.
"""

from __future__ import annotations

import datetime as _dt

from storage_validator.backends.base import get_backend
from storage_validator.config import Config
from storage_validator.models import Report


def run(cfg: Config) -> Report:
    """Run a full validation pass (discovery + health + optional perf)."""
    # Importing the backends package registers all built-in backends
    # (lustre, and future others) with BACKEND_REGISTRY as a side effect.
    import storage_validator.backends.lustre  # noqa: F401

    backend_cls = get_backend(cfg.backend)
    backend = backend_cls(health_cfg=cfg.health)

    topology = backend.discover()
    health_results = backend.run_health_checks(topology)
    perf_results = [] if cfg.skip_perf else backend.run_perf_checks(topology, cfg.perf)

    return Report(
        fsname=topology.fsname,
        timestamp=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        topology=topology,
        health=health_results,
        perf=perf_results,
    )
