"""LustreBackend: wires discovery/health/perf modules behind StorageBackend."""

from __future__ import annotations

from storage_validator.backends.base import StorageBackend, register_backend
from storage_validator.config import HealthConfig, PerfConfig
from storage_validator.models import CheckResult, PerfResult, Topology

from . import discovery, health, perf


@register_backend("lustre")
class LustreBackend(StorageBackend):
    def __init__(self, health_cfg: HealthConfig | None = None):
        self.health_cfg = health_cfg or HealthConfig()

    def discover(self) -> Topology:
        return discovery.discover_topology()

    def run_health_checks(self, topology: Topology) -> list[CheckResult]:
        return health.run_health_checks(
            topology,
            warn_pct=self.health_cfg.warn_pct,
            fail_pct=self.health_cfg.fail_pct,
        )

    def run_perf_checks(self, topology: Topology, cfg: PerfConfig) -> list[PerfResult]:
        mount_path = cfg.mount_path or (topology.mounts[0] if topology.mounts else None)
        if mount_path is None:
            return [
                PerfResult(
                    target="(all)",
                    kind="throughput",
                    value=0.0,
                    unit="MB/s",
                    status="FAIL",
                    message="no mount_path configured and no lustre client mount found",
                )
            ]
        return perf.run_perf_checks(
            topology,
            mount_path,
            size=cfg.perf_size,
            block_size=cfg.perf_block_size,
            runtime=cfg.perf_runtime,
            timeout=cfg.timeout,
            default_thresholds=cfg.default_thresholds,
            pool_thresholds=cfg.pool_thresholds,
            elbencho_path=cfg.elbencho_path,
            threads=cfg.perf_threads,
        )
