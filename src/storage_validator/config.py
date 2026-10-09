"""Configuration dataclasses for health thresholds, perf parameters, and the
overall validation run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HealthConfig:
    warn_pct: int = 80
    fail_pct: int = 95


@dataclass
class PerfThresholds:
    """Throughput/latency pass/warn/fail thresholds for one drive class
    (e.g. a Lustre OST pool such as "ssd" or "hdd"). Different drive types
    typically have very different expected performance, so each pool can
    be given its own thresholds instead of one set for the whole filesystem.
    """

    warn_mbps: float = 200.0
    fail_mbps: float = 50.0
    warn_ms: float = 10.0
    fail_ms: float = 50.0


@dataclass
class PerfConfig:
    mount_path: Optional[str] = None
    size_mb: int = 256
    timeout: float = 60
    warn_mbps: float = 200.0
    fail_mbps: float = 50.0
    warn_ms: float = 10.0
    fail_ms: float = 50.0
    # Per-pool threshold overrides, keyed by Lustre OST pool name. An OST
    # belonging to a pool present here uses these thresholds instead of the
    # defaults above; OSTs not in any pool (or in a pool with no override)
    # use the defaults above.
    pool_thresholds: dict[str, PerfThresholds] = field(default_factory=dict)
    # MiB each parallel writer contributes to a pool's aggregate throughput
    # test (total aggregate test size = this * number of OSTs in the pool).
    # Defaults to `size_mb` when not set.
    pool_size_mb_per_ost: Optional[int] = None
    # Path to the `elbencho` binary, used for both per-OST and per-pool perf
    # checks. Defaults to "elbencho" (resolved via PATH) when not set.
    elbencho_path: str = "elbencho"
    # Worker thread count elbencho uses per test file, for both the per-OST
    # tests (one file, this many threads) and the per-pool tests (each
    # pool/OST file gets this many threads). None means auto-detect from the
    # host's total CPU threads (via `lscpu`).
    perf_threads: Optional[int] = None

    @property
    def default_thresholds(self) -> PerfThresholds:
        return PerfThresholds(
            warn_mbps=self.warn_mbps,
            fail_mbps=self.fail_mbps,
            warn_ms=self.warn_ms,
            fail_ms=self.fail_ms,
        )


@dataclass
class Config:
    backend: str = "lustre"
    skip_perf: bool = False
    health: HealthConfig = field(default_factory=HealthConfig)
    perf: PerfConfig = field(default_factory=PerfConfig)
    output_json: Optional[str] = None
