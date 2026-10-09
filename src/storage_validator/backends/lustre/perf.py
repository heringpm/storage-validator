"""Basic per-OST throughput/latency perf checks using `lfs setstripe` + `dd`.

Each check writes a scratch file explicitly striped onto a single target OST
(`lfs setstripe -i <index> -c 1`), measures `dd`'s reported transfer rate
(throughput) or per-write latency, then removes the file. All subprocess
calls go through `shell.run_cmd` so they can be mocked in tests.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict

from storage_validator.config import PerfThresholds
from storage_validator.models import PerfResult, Target, Topology

from . import shell

log = logging.getLogger(__name__)

DEFAULT_SIZE_MB = 256
DEFAULT_WARN_MBPS = 200.0
DEFAULT_FAIL_MBPS = 50.0
DEFAULT_WARN_MS = 10.0
DEFAULT_FAIL_MS = 50.0

_OST_INDEX_RE = re.compile(r"OST([0-9a-fA-F]+)$")

# e.g. "1048576 bytes (1.0 MB, 1.0 MiB) copied, 0.0123 s, 84.9 MB/s"
_DD_RATE_RE = re.compile(
    r"copied,\s*(?P<seconds>[\d.]+)\s*s,\s*(?P<rate>[\d.]+)\s*(?P<unit>[kKmMgG]?B/s)"
)

_UNIT_TO_MBPS = {
    "B/s": 1 / (1024 * 1024),
    "kB/s": 1 / 1024,
    "KB/s": 1 / 1024,
    "MB/s": 1.0,
    "GB/s": 1024.0,
}


def ost_index(target: Target) -> int | None:
    """Extract the numeric OST index from a target name like `fs-OST0003`."""
    match = _OST_INDEX_RE.search(target.name)
    if not match:
        return None
    return int(match.group(1), 16)


def parse_dd_rate_mbps(dd_stderr: str) -> float | None:
    """Parse `dd`'s "copied, Ns, R MB/s" trailer into a MB/s float."""
    match = _DD_RATE_RE.search(dd_stderr)
    if not match:
        return None
    rate = float(match.group("rate"))
    factor = _UNIT_TO_MBPS.get(match.group("unit"))
    if factor is None:
        return None
    return rate * factor


def throughput_check(
    target: Target,
    mount_path: str,
    size_mb: int = DEFAULT_SIZE_MB,
    timeout: float = 60,
    warn_mbps: float = DEFAULT_WARN_MBPS,
    fail_mbps: float = DEFAULT_FAIL_MBPS,
) -> PerfResult:
    """Write `size_mb` MiB directly to `target`'s OST and measure throughput."""
    idx = ost_index(target)
    if idx is None:
        return PerfResult(
            target=target.name,
            kind="throughput",
            value=0.0,
            unit="MB/s",
            status="FAIL",
            message=f"could not determine OST index from name {target.name!r}",
        )

    path = os.path.join(mount_path, f".storage_validator_perf_{target.name}")
    try:
        setstripe = shell.run_cmd(
            ["lfs", "setstripe", "-i", str(idx), "-c", "1", path], timeout=timeout
        )
        if not setstripe.ok:
            return PerfResult(
                target=target.name,
                kind="throughput",
                value=0.0,
                unit="MB/s",
                status="FAIL",
                message=f"lfs setstripe failed: {setstripe.stderr.strip()}",
            )

        dd_result = shell.run_cmd(
            [
                "dd",
                "if=/dev/zero",
                f"of={path}",
                "bs=1M",
                f"count={size_mb}",
                "oflag=direct",
            ],
            timeout=timeout,
        )
        rate = parse_dd_rate_mbps(dd_result.stdout + dd_result.stderr)
        if not dd_result.ok or rate is None:
            return PerfResult(
                target=target.name,
                kind="throughput",
                value=0.0,
                unit="MB/s",
                status="FAIL",
                message=f"dd failed or unparsable output: {dd_result.stderr.strip()}",
            )

        if rate < fail_mbps:
            status, msg = "FAIL", f"{rate:.1f} MB/s below fail threshold {fail_mbps}"
        elif rate < warn_mbps:
            status, msg = "WARN", f"{rate:.1f} MB/s below warn threshold {warn_mbps}"
        else:
            status, msg = "PASS", f"{rate:.1f} MB/s"
        return PerfResult(
            target=target.name, kind="throughput", value=rate, unit="MB/s",
            status=status, message=msg,
        )
    except shell.CommandError as exc:
        return PerfResult(
            target=target.name, kind="throughput", value=0.0, unit="MB/s",
            status="FAIL", message=str(exc),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def latency_check(
    target: Target,
    mount_path: str,
    timeout: float = 30,
    warn_ms: float = DEFAULT_WARN_MS,
    fail_ms: float = DEFAULT_FAIL_MS,
) -> PerfResult:
    """Measure single 4K synced-write latency on `target`'s OST via `dd`."""
    idx = ost_index(target)
    if idx is None:
        return PerfResult(
            target=target.name, kind="latency", value=0.0, unit="ms",
            status="FAIL",
            message=f"could not determine OST index from name {target.name!r}",
        )

    path = os.path.join(mount_path, f".storage_validator_lat_{target.name}")
    try:
        setstripe = shell.run_cmd(
            ["lfs", "setstripe", "-i", str(idx), "-c", "1", path], timeout=timeout
        )
        if not setstripe.ok:
            return PerfResult(
                target=target.name, kind="latency", value=0.0, unit="ms",
                status="FAIL", message=f"lfs setstripe failed: {setstripe.stderr.strip()}",
            )

        dd_result = shell.run_cmd(
            [
                "dd", "if=/dev/zero", f"of={path}", "bs=4k", "count=1",
                "oflag=direct,sync",
            ],
            timeout=timeout,
        )
        rate_match = _DD_RATE_RE.search(dd_result.stdout + dd_result.stderr)
        if not dd_result.ok or not rate_match:
            return PerfResult(
                target=target.name, kind="latency", value=0.0, unit="ms",
                status="FAIL", message=f"dd failed or unparsable output: {dd_result.stderr.strip()}",
            )
        latency_ms = float(rate_match.group("seconds")) * 1000.0

        if latency_ms > fail_ms:
            status, msg = "FAIL", f"{latency_ms:.2f} ms above fail threshold {fail_ms}"
        elif latency_ms > warn_ms:
            status, msg = "WARN", f"{latency_ms:.2f} ms above warn threshold {warn_ms}"
        else:
            status, msg = "PASS", f"{latency_ms:.2f} ms"
        return PerfResult(
            target=target.name, kind="latency", value=latency_ms, unit="ms",
            status=status, message=msg,
        )
    except shell.CommandError as exc:
        return PerfResult(
            target=target.name, kind="latency", value=0.0, unit="ms",
            status="FAIL", message=str(exc),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def resolve_thresholds(
    target: Target,
    default: PerfThresholds,
    pool_thresholds: dict[str, PerfThresholds] | None,
) -> PerfThresholds:
    """Pick the thresholds for `target`: its pool's override if one exists
    (and the pool has an override configured), else the filesystem default.
    """
    if target.pool and pool_thresholds and target.pool in pool_thresholds:
        return pool_thresholds[target.pool]
    return default


def _aggregate_pool_result(
    pool_label: str,
    kind: str,
    unit: str,
    values: list[float],
    warn: float,
    fail: float,
    worse_when_higher: bool,
) -> PerfResult:
    """Build a pool-level PerfResult from the mean of its per-OST values."""
    avg = sum(values) / len(values)
    if worse_when_higher:
        if avg > fail:
            status, msg = "FAIL", f"avg {avg:.2f} {unit} above fail threshold {fail}"
        elif avg > warn:
            status, msg = "WARN", f"avg {avg:.2f} {unit} above warn threshold {warn}"
        else:
            status, msg = "PASS", f"avg {avg:.2f} {unit}"
    else:
        if avg < fail:
            status, msg = "FAIL", f"avg {avg:.1f} {unit} below fail threshold {fail}"
        elif avg < warn:
            status, msg = "WARN", f"avg {avg:.1f} {unit} below warn threshold {warn}"
        else:
            status, msg = "PASS", f"avg {avg:.1f} {unit}"
    return PerfResult(
        target=f"pool:{pool_label}",
        kind=kind,
        value=avg,
        unit=unit,
        status=status,
        message=msg,
        pool=pool_label if pool_label != "(unpooled)" else None,
        scope="pool",
    )


def run_perf_checks(
    topology: Topology,
    mount_path: str,
    size_mb: int = DEFAULT_SIZE_MB,
    timeout: float = 60,
    default_thresholds: PerfThresholds | None = None,
    pool_thresholds: dict[str, PerfThresholds] | None = None,
) -> list[PerfResult]:
    """Run throughput + latency checks against every OST in the topology,
    then add one aggregate throughput+latency PerfResult per OST pool (plus
    one for any OSTs that aren't in a pool), using each pool's own
    thresholds so different drive types (e.g. ssd vs hdd pools) aren't
    judged against the same bar.
    """
    default_thresholds = default_thresholds or PerfThresholds()
    results: list[PerfResult] = []
    by_pool: dict[str, list[PerfResult]] = defaultdict(list)
    for target in topology.osts:
        th = resolve_thresholds(target, default_thresholds, pool_thresholds)
        t_result = throughput_check(
            target, mount_path, size_mb, timeout,
            warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
        )
        l_result = latency_check(
            target, mount_path, timeout=timeout,
            warn_ms=th.warn_ms, fail_ms=th.fail_ms,
        )
        t_result.pool = target.pool
        l_result.pool = target.pool
        results.append(t_result)
        results.append(l_result)
        pool_label = target.pool or "(unpooled)"
        by_pool[pool_label].append(t_result)
        by_pool[pool_label].append(l_result)

    for pool_label, pool_results in by_pool.items():
        th = (pool_thresholds or {}).get(pool_label, default_thresholds)
        throughput_values = [r.value for r in pool_results if r.kind == "throughput"]
        latency_values = [r.value for r in pool_results if r.kind == "latency"]
        if throughput_values:
            results.append(
                _aggregate_pool_result(
                    pool_label, "throughput", "MB/s", throughput_values,
                    th.warn_mbps, th.fail_mbps, worse_when_higher=False,
                )
            )
        if latency_values:
            results.append(
                _aggregate_pool_result(
                    pool_label, "latency", "ms", latency_values,
                    th.warn_ms, th.fail_ms, worse_when_higher=True,
                )
            )
    return results
