"""Per-OST and per-pool throughput/latency perf checks.

Per-OST checks write a scratch file explicitly striped onto a single target
OST (`lfs setstripe -i <index> -c 1`) and measure `dd`'s reported transfer
rate (throughput) or per-write latency. `dd` is single-threaded, which is
fine for a single-OST sanity check.

Per-pool checks (`pool_throughput_check`/`pool_latency_check`) measure real
aggregate performance using `elbencho`, a multi-threaded benchmark tool: one
scratch file is pre-striped onto each OST in the pool individually (`lfs
setstripe -i <idx> -c 1`), then `elbencho` is run once with one worker thread
per file, driving every OST in the pool concurrently. This is not an average
of the independent per-OST `dd` results above — it's a distinct test that
exercises real parallel/aggregate I/O across the pool using actual
multi-threaded writers instead of a single `dd` process.

All subprocess calls go through `shell.run_cmd` so they can be mocked in
tests.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
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


def _pool_fail(pool_label: str, kind: str, unit: str, message: str) -> PerfResult:
    return PerfResult(
        target=f"pool:{pool_label}", kind=kind, value=0.0, unit=unit,
        status="FAIL", message=message,
        pool=pool_label if pool_label != "(unpooled)" else None, scope="pool",
    )


def _pool_ost_paths(pool_label: str, indices: list[int], mount_path: str, suffix: str) -> list[str]:
    """One dedicated scratch file per OST index, so each can be independently
    single-striped onto its own OST and then driven by its own elbencho
    worker thread.
    """
    return [
        os.path.join(mount_path, f".storage_validator_pool_{suffix}_{pool_label}_{idx}")
        for idx in indices
    ]


def _setstripe_per_ost(indices: list[int], paths: list[str], timeout: float) -> str | None:
    """Single-stripe every path onto its matching OST index.

    Returns an error message if any `lfs setstripe` call fails, else None.
    """
    for idx, path in zip(indices, paths):
        result = shell.run_cmd(
            ["lfs", "setstripe", "-i", str(idx), "-c", "1", path], timeout=timeout
        )
        if not result.ok:
            return f"lfs setstripe failed for OST {idx}: {result.stderr.strip()}"
    return None


def _read_elbencho_csv_last_row(csv_path: str) -> dict[str, str] | None:
    """Read the last row elbencho wrote to its `--csvfile` output."""
    try:
        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    return rows[-1] if rows else None


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
    """Measure real aggregate throughput across every OST in a pool using
    `elbencho`.

    Each OST gets its own scratch file single-striped onto it (`lfs
    setstripe -i <idx> -c 1`), then `elbencho` is run once with one worker
    thread per file (`-t <stripe_count>`), so every OST is driven
    concurrently by a real multi-threaded writer instead of a single `dd`
    process. The reported aggregate MiB/s ("MiB/s [last]" in elbencho's
    output, i.e. the aggregate rate once the slowest/last thread finishes)
    is used as the pool's throughput — not an average of independent
    single-OST results.
    """
    indices = _group_ost_indices(targets)
    if not indices:
        return _pool_fail(
            pool_label, "throughput", "MB/s",
            "could not determine any OST indices in pool",
        )

    stripe_count = len(indices)
    paths = _pool_ost_paths(pool_label, indices, mount_path, "perf")
    csv_fd, csv_path = tempfile.mkstemp(prefix="storage_validator_elbencho_", suffix=".csv")
    os.close(csv_fd)
    os.remove(csv_path)
    try:
        err = _setstripe_per_ost(indices, paths, timeout)
        if err:
            return _pool_fail(pool_label, "throughput", "MB/s", err)

        result = shell.run_cmd(
            [
                "elbencho", "-w", "-t", str(stripe_count), "-b", "1m",
                "-s", f"{size_mb_per_ost}m", "--direct",
                "--csvfile", csv_path,
            ] + paths,
            timeout=timeout,
        )
        if not result.ok:
            return _pool_fail(
                pool_label, "throughput", "MB/s",
                f"elbencho failed: {(result.stderr or result.stdout).strip()}",
            )

        row = _read_elbencho_csv_last_row(csv_path)
        if row is None or "MiB/s [last]" not in row:
            return _pool_fail(
                pool_label, "throughput", "MB/s",
                "could not parse elbencho CSV output",
            )
        rate = float(row["MiB/s [last]"])

        total_mb = stripe_count * size_mb_per_ost
        detail = f"{stripe_count} OSTs, {total_mb} MiB via elbencho ({stripe_count} threads)"
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
        return _pool_fail(pool_label, "throughput", "MB/s", str(exc))
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.remove(csv_path)
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
    """Measure worst-case write latency across a pool under concurrent load
    using `elbencho`.

    Each OST gets its own scratch file (same per-OST striping as
    `pool_throughput_check`), and `elbencho` issues one small 4K direct write
    per OST concurrently (`-t <stripe_count>`, `-b 4k -s 4k`). The reported
    max IO latency ("IO lat us [max]") across all worker threads is used as
    the pool's latency, since that tail latency is what a client actually
    experiences when an I/O touches every stripe of a wide file.
    """
    indices = _group_ost_indices(targets)
    if not indices:
        return _pool_fail(
            pool_label, "latency", "ms",
            "could not determine any OST indices in pool",
        )

    stripe_count = len(indices)
    paths = _pool_ost_paths(pool_label, indices, mount_path, "lat")
    csv_fd, csv_path = tempfile.mkstemp(prefix="storage_validator_elbencho_", suffix=".csv")
    os.close(csv_fd)
    os.remove(csv_path)
    try:
        err = _setstripe_per_ost(indices, paths, timeout)
        if err:
            return _pool_fail(pool_label, "latency", "ms", err)

        result = shell.run_cmd(
            [
                "elbencho", "-w", "-t", str(stripe_count), "-b", "4k",
                "-s", "4k", "--direct",
                "--csvfile", csv_path,
            ] + paths,
            timeout=timeout,
        )
        if not result.ok:
            return _pool_fail(
                pool_label, "latency", "ms",
                f"elbencho failed: {(result.stderr or result.stdout).strip()}",
            )

        row = _read_elbencho_csv_last_row(csv_path)
        if row is None or "IO lat us [max]" not in row:
            return _pool_fail(
                pool_label, "latency", "ms",
                "could not parse elbencho CSV output",
            )
        worst_ms = float(row["IO lat us [max]"]) / 1000.0

        detail = f"worst of {stripe_count} concurrent OST writes via elbencho"
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
        return _pool_fail(pool_label, "latency", "ms", str(exc))
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.remove(csv_path)
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
    one for any OSTs that aren't in a pool) using `elbencho`: one
    single-striped scratch file per OST, driven concurrently by one
    elbencho worker thread per file, so the pool result reflects actual
    multi-threaded aggregate bandwidth/tail latency rather than an average
    of independent single-OST `dd` tests. Each pool's own thresholds are
    used so different drive types (e.g. ssd vs hdd pools) aren't judged
    against the same bar.
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
