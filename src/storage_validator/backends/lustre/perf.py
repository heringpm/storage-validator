"""Per-OST and per-pool throughput/latency perf checks using `lfs setstripe`
+ `dd`.

Per-OST checks write a scratch file explicitly striped onto a single target
OST (`lfs setstripe -i <index> -c 1`) and measure `dd`'s reported transfer
rate (throughput) or per-write latency.

Per-pool checks (`pool_throughput_check`/`pool_latency_check`) measure real
aggregate performance: one file is striped across every OST in the pool
(`lfs setstripe -p <pool>` or explicit `-o <indices>` for unpooled OSTs),
then written concurrently with one `dd` process per stripe so every OST in
the pool is driven at the same time. This is not an average of the
independent per-OST results above — it's a distinct test that exercises
real parallel/aggregate I/O across the pool.

All subprocess calls go through `shell.run_cmd` so they can be mocked in
tests.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from storage_validator.config import PerfThresholds
from storage_validator.models import PerfResult, Target, Topology

from . import shell

MAX_POOL_WORKERS = 16

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


def _group_ost_indices(targets: list[Target]) -> list[int]:
    """OST indices for a pool/group, sorted, skipping unparsable names."""
    indices = []
    for target in targets:
        idx = ost_index(target)
        if idx is not None:
            indices.append(idx)
        else:
            log.warning("skipping %s: could not determine OST index", target.name)
    return sorted(indices)


def _pool_setstripe_cmd(
    pool_label: str, fsname: str, indices: list[int], path: str
) -> list[str]:
    """Stripe `path` across every OST index in this group.

    For a real Lustre OST pool, `-p <fsname>.<pool>` lets Lustre pick the
    member OSTs. For the synthetic "(unpooled)" group (OSTs with no pool
    membership), there is no pool name to pass, so the exact OST indices are
    given explicitly via `-o`.
    """
    stripe_count = len(indices)
    if pool_label != "(unpooled)":
        return [
            "lfs", "setstripe", "-p", f"{fsname}.{pool_label}",
            "-c", str(stripe_count), path,
        ]
    return [
        "lfs", "setstripe", "-o", ",".join(str(i) for i in indices),
        "-c", str(stripe_count), path,
    ]


def pool_throughput_check(
    pool_label: str,
    targets: list[Target],
    fsname: str,
    mount_path: str,
    size_mb_per_ost: int = DEFAULT_SIZE_MB,
    timeout: float = 60,
    warn_mbps: float = DEFAULT_WARN_MBPS,
    fail_mbps: float = DEFAULT_FAIL_MBPS,
) -> PerfResult:
    """Measure real aggregate throughput across every OST in a pool.

    Writes ONE file striped across all of the pool's OSTs (`lfs setstripe
    -c <stripe_count>`), then writes it with one concurrent `dd` process per
    stripe (each targeting its own stripe-aligned byte range), so all OSTs
    are driven at the same time. Aggregate MB/s is computed from total bytes
    written / wall-clock time of the whole parallel batch — not an average
    of independent single-OST results.
    """
    indices = _group_ost_indices(targets)
    if not indices:
        return PerfResult(
            target=f"pool:{pool_label}", kind="throughput", value=0.0, unit="MB/s",
            status="FAIL", message="could not determine any OST indices in pool",
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )

    stripe_count = len(indices)
    path = os.path.join(mount_path, f".storage_validator_pool_perf_{pool_label}")
    try:
        setstripe = shell.run_cmd(
            _pool_setstripe_cmd(pool_label, fsname, indices, path), timeout=timeout
        )
        if not setstripe.ok:
            return PerfResult(
                target=f"pool:{pool_label}", kind="throughput", value=0.0, unit="MB/s",
                status="FAIL", message=f"lfs setstripe failed: {setstripe.stderr.strip()}",
                pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
            )

        def _write_stripe(i: int) -> shell.CommandResult:
            return shell.run_cmd(
                [
                    "dd", "if=/dev/zero", f"of={path}", "bs=1M",
                    f"count={size_mb_per_ost}", f"seek={i * size_mb_per_ost}",
                    "oflag=direct", "conv=notrunc",
                ],
                timeout=timeout,
            )

        workers = min(stripe_count, MAX_POOL_WORKERS)
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            dd_results = list(pool.map(_write_stripe, range(stripe_count)))
        elapsed = max(time.monotonic() - start, 1e-6)

        failed = [r for r in dd_results if not r.ok]
        if failed:
            return PerfResult(
                target=f"pool:{pool_label}", kind="throughput", value=0.0, unit="MB/s",
                status="FAIL",
                message=f"{len(failed)}/{stripe_count} parallel dd writes failed: "
                f"{failed[0].stderr.strip()}",
                pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
            )

        total_mb = stripe_count * size_mb_per_ost
        rate = total_mb / elapsed
        detail = f"{stripe_count} OSTs, {total_mb} MiB in {elapsed:.2f}s"
        if rate < fail_mbps:
            status = "FAIL"
            msg = f"{rate:.1f} MB/s aggregate below fail threshold {fail_mbps} ({detail})"
        elif rate < warn_mbps:
            status = "WARN"
            msg = f"{rate:.1f} MB/s aggregate below warn threshold {warn_mbps} ({detail})"
        else:
            status, msg = "PASS", f"{rate:.1f} MB/s aggregate ({detail})"
        return PerfResult(
            target=f"pool:{pool_label}", kind="throughput", value=rate, unit="MB/s",
            status=status, message=msg,
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )
    except shell.CommandError as exc:
        return PerfResult(
            target=f"pool:{pool_label}", kind="throughput", value=0.0, unit="MB/s",
            status="FAIL", message=str(exc),
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def pool_latency_check(
    pool_label: str,
    targets: list[Target],
    fsname: str,
    mount_path: str,
    timeout: float = 30,
    warn_ms: float = DEFAULT_WARN_MS,
    fail_ms: float = DEFAULT_FAIL_MS,
) -> PerfResult:
    """Measure worst-case write latency across a pool under concurrent load.

    Issues one small synced 4K write per OST in the pool at the same time
    (same striping approach as `pool_throughput_check`) and reports the
    slowest one, since that tail latency is what a client actually
    experiences when an I/O touches every stripe of a wide file.
    """
    indices = _group_ost_indices(targets)
    if not indices:
        return PerfResult(
            target=f"pool:{pool_label}", kind="latency", value=0.0, unit="ms",
            status="FAIL", message="could not determine any OST indices in pool",
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )

    stripe_count = len(indices)
    path = os.path.join(mount_path, f".storage_validator_pool_lat_{pool_label}")
    try:
        setstripe = shell.run_cmd(
            _pool_setstripe_cmd(pool_label, fsname, indices, path), timeout=timeout
        )
        if not setstripe.ok:
            return PerfResult(
                target=f"pool:{pool_label}", kind="latency", value=0.0, unit="ms",
                status="FAIL", message=f"lfs setstripe failed: {setstripe.stderr.strip()}",
                pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
            )

        def _write_stripe(i: int) -> shell.CommandResult:
            return shell.run_cmd(
                [
                    "dd", "if=/dev/zero", f"of={path}", "bs=4k", "count=1",
                    f"seek={i}", "oflag=direct,sync", "conv=notrunc",
                ],
                timeout=timeout,
            )

        workers = min(stripe_count, MAX_POOL_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            dd_results = list(pool.map(_write_stripe, range(stripe_count)))

        failed = [r for r in dd_results if not r.ok]
        if failed:
            return PerfResult(
                target=f"pool:{pool_label}", kind="latency", value=0.0, unit="ms",
                status="FAIL",
                message=f"{len(failed)}/{stripe_count} parallel dd writes failed: "
                f"{failed[0].stderr.strip()}",
                pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
            )

        latencies_ms = []
        for r in dd_results:
            match = _DD_RATE_RE.search(r.stdout + r.stderr)
            if match:
                latencies_ms.append(float(match.group("seconds")) * 1000.0)
        if not latencies_ms:
            return PerfResult(
                target=f"pool:{pool_label}", kind="latency", value=0.0, unit="ms",
                status="FAIL", message="dd output unparsable for all parallel writes",
                pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
            )

        worst_ms = max(latencies_ms)
        detail = f"worst of {stripe_count} concurrent OST writes"
        if worst_ms > fail_ms:
            status = "FAIL"
            msg = f"{worst_ms:.2f} ms above fail threshold {fail_ms} ({detail})"
        elif worst_ms > warn_ms:
            status = "WARN"
            msg = f"{worst_ms:.2f} ms above warn threshold {warn_ms} ({detail})"
        else:
            status, msg = "PASS", f"{worst_ms:.2f} ms ({detail})"
        return PerfResult(
            target=f"pool:{pool_label}", kind="latency", value=worst_ms, unit="ms",
            status=status, message=msg,
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )
    except shell.CommandError as exc:
        return PerfResult(
            target=f"pool:{pool_label}", kind="latency", value=0.0, unit="ms",
            status="FAIL", message=str(exc),
            pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def run_perf_checks(
    topology: Topology,
    mount_path: str,
    size_mb: int = DEFAULT_SIZE_MB,
    timeout: float = 60,
    default_thresholds: PerfThresholds | None = None,
    pool_thresholds: dict[str, PerfThresholds] | None = None,
    pool_size_mb_per_ost: int | None = None,
) -> list[PerfResult]:
    """Run throughput + latency checks against every OST in the topology,
    then run one real aggregate throughput+latency test per OST pool (plus
    one for any OSTs that aren't in a pool): a single file striped across
    every OST in the group, written concurrently (one writer per stripe), so
    the pool result reflects actual aggregate bandwidth/tail latency rather
    than an average of independent single-OST tests. Each pool's own
    thresholds are used so different drive types (e.g. ssd vs hdd pools)
    aren't judged against the same bar.
    """
    default_thresholds = default_thresholds or PerfThresholds()
    pool_size_mb_per_ost = pool_size_mb_per_ost or size_mb
    results: list[PerfResult] = []
    groups: dict[str, list[Target]] = defaultdict(list)
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
        groups[pool_label].append(target)

    for pool_label, group_targets in groups.items():
        th = (pool_thresholds or {}).get(pool_label, default_thresholds)
        results.append(
            pool_throughput_check(
                pool_label, group_targets, topology.fsname, mount_path,
                size_mb_per_ost=pool_size_mb_per_ost, timeout=timeout,
                warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
            )
        )
        results.append(
            pool_latency_check(
                pool_label, group_targets, topology.fsname, mount_path,
                timeout=timeout, warn_ms=th.warn_ms, fail_ms=th.fail_ms,
            )
        )
    return results
