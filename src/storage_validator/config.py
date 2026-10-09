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
    # Subprocess timeout (s) for each `lfs`/elbencho call. Not the elbencho
    # test runtime itself -- see `perf_runtime` below.
    timeout: float = 90
    warn_mbps: float = 200.0
    fail_mbps: float = 50.0
    warn_ms: float = 10.0
    fail_ms: float = 50.0
    # Per-pool threshold overrides, keyed by Lustre OST pool name. An OST
    # belonging to a pool present here uses these thresholds instead of the
    # defaults above; OSTs not in any pool (or in a pool with no override)
    # use the defaults above.
    pool_thresholds: dict[str, PerfThresholds] = field(default_factory=dict)
    # Path to the `elbencho` binary, used for both per-OST and per-pool perf
    # checks. Defaults to "elbencho" (resolved via PATH) when not set.
    elbencho_path: str = "elbencho"
    # Total elbencho worker thread count for a single test run, whether that
    # run targets one OST (per-OST check) or every OST in a pool at once
    # (per-pool check). This is never multiplied by the number of OSTs in a
    # pool -- a single elbencho invocation always uses exactly this many
    # threads in total. None means auto-detect from the host's total CPU
    # threads (via `lscpu`).
    perf_threads: Optional[int] = None
    # elbencho `-s` test file size (e.g. "1g"), large enough that the test
    # isn't just measuring client-side page cache.
    perf_size: str = "1g"
    # elbencho `-b` block size (e.g. "1m").
    perf_block_size: str = "1m"
    # elbencho `--timelimit` in seconds, applied to each read/write test run.
    perf_runtime: int = 60
    # If non-empty, restrict per-OST perf checks to only these OST names
    # (e.g. {"scratch-OST0000"}). Per-pool checks are unaffected -- see
    # `pool_names` below.
    ost_names: Optional[set[str]] = None
    # If non-empty, restrict per-pool perf checks to only these pool names.
    # Per-OST checks are unaffected -- see `ost_names` above.
    pool_names: Optional[set[str]] = None

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
    dry_run: bool = False
    health: HealthConfig = field(default_factory=HealthConfig)
    perf: PerfConfig = field(default_factory=PerfConfig)
    output_json: Optional[str] = None
