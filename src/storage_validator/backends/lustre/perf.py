"""Per-OST and per-pool read/write throughput+latency perf checks.

Both per-OST checks (`ost_rw_check`) and per-pool checks (`pool_rw_check`)
measure performance using `elbencho`, a multi-threaded direct-I/O benchmark
tool. Each check runs one elbencho invocation per I/O direction (write, then
read) and derives *both* the throughput ("MiB/s [last]") and the latency
("IO lat us [max]") from that single run's CSV output -- there is no
separate latency-only test.

A single elbencho invocation -- whether it targets one OST (per-OST check)
or every OST in a pool at once (per-pool check, one scratch file per OST
passed as multiple paths) -- always uses exactly `threads` worker threads in
total. It is never multiplied by the number of OSTs in a pool, so a run on a
single system never uses more worker threads than the configured/detected
CPU thread count.

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
from typing import Literal

from storage_validator.config import PerfThresholds
from storage_validator.models import PerfResult, Target, Topology

from . import shell

log = logging.getLogger(__name__)

IoMode = Literal["read", "write"]

DEFAULT_SIZE = "1g"
DEFAULT_BLOCK_SIZE = "1m"
DEFAULT_RUNTIME = 60
DEFAULT_WARN_MBPS = 200.0
DEFAULT_FAIL_MBPS = 50.0
DEFAULT_WARN_MS = 10.0
DEFAULT_FAIL_MS = 50.0

_OST_INDEX_RE = re.compile(r"OST([0-9a-fA-F]+)$")


class ElbenchoError(Exception):
    """elbencho ran but failed, or its CSV output couldn't be parsed."""


def detect_cpu_thread_count() -> int:
    """Total CPU threads on this host, used as the default elbencho worker
    thread count for perf checks when `--perf-threads` isn't given.

    Parses the "CPU(s):" line from `lscpu`; falls back to `os.cpu_count()`
    (and then 1) if `lscpu` is unavailable or its output can't be parsed.
    """
    try:
        result = shell.run_cmd(["lscpu"], timeout=5)
        if result.ok:
            for line in result.stdout.splitlines():
                if line.strip().startswith("CPU(s):"):
                    return int(line.split(":", 1)[1].strip())
    except (shell.CommandError, ValueError):
        pass
    return os.cpu_count() or 1


def ost_index(target: Target) -> int | None:
    """Extract the numeric OST index from a target name like `fs-OST0003`."""
    match = _OST_INDEX_RE.search(target.name)
    if not match:
        return None
    return int(match.group(1), 16)


def _read_elbencho_csv_last_row(csv_path: str) -> dict[str, str] | None:
    """Read the last row elbencho wrote to its `--csvfile` output."""
    try:
        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    return rows[-1] if rows else None


def _run_elbencho_rw(
    paths: list[str],
    mode: IoMode,
    threads: int,
    size: str,
    block_size: str,
    runtime: int,
    elbencho_path: str,
    timeout: float,
) -> tuple[float, float]:
    """Run one elbencho read or write pass against `paths` with a total of
    `threads` worker threads (regardless of how many paths are given), and
    return `(throughput_mibs, latency_us)` parsed from its CSV output.

    Raises `ElbenchoError` if the command fails or its output can't be
    parsed, `shell.CommandError` if the command itself couldn't be run.
    """
    io_flag = "-w" if mode == "write" else "-r"
    csv_fd, csv_path = tempfile.mkstemp(prefix="storage_validator_elbencho_", suffix=".csv")
    os.close(csv_fd)
    os.remove(csv_path)
    try:
        result = shell.run_cmd(
            [
                elbencho_path, io_flag, "-t", str(threads), "-b", block_size,
                "-s", size, "--direct", "--timelimit", str(runtime),
                "--csvfile", csv_path,
            ] + paths,
            timeout=timeout,
        )
        if not result.ok:
            raise ElbenchoError((result.stderr or result.stdout).strip())

        row = _read_elbencho_csv_last_row(csv_path)
        if row is None or "MiB/s [last]" not in row or "IO lat us [max]" not in row:
            raise ElbenchoError("could not parse elbencho CSV output")
        return float(row["MiB/s [last]"]), float(row["IO lat us [max]"])
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


def _classify_throughput(rate: float, warn_mbps: float, fail_mbps: float) -> tuple[str, str]:
    if rate < fail_mbps:
        return "FAIL", f"{rate:.1f} MB/s below fail threshold {fail_mbps}"
    if rate < warn_mbps:
        return "WARN", f"{rate:.1f} MB/s below warn threshold {warn_mbps}"
    return "PASS", f"{rate:.1f} MB/s"


def _classify_latency(latency_ms: float, warn_ms: float, fail_ms: float) -> tuple[str, str]:
    if latency_ms > fail_ms:
        return "FAIL", f"{latency_ms:.2f} ms above fail threshold {fail_ms}"
    if latency_ms > warn_ms:
        return "WARN", f"{latency_ms:.2f} ms above warn threshold {warn_ms}"
    return "PASS", f"{latency_ms:.2f} ms"


def _fail_pair(
    target_name: str, mode: IoMode, message: str,
    pool: str | None = None, scope: Literal["ost", "pool"] = "ost",
) -> tuple[PerfResult, PerfResult]:
    common = dict(target=target_name, status="FAIL", message=message, pool=pool, scope=scope, io_mode=mode)
    return (
        PerfResult(kind="throughput", value=0.0, unit="MB/s", **common),
        PerfResult(kind="latency", value=0.0, unit="ms", **common),
    )


def ost_rw_check(
    target: Target,
    mount_path: str,
    mode: IoMode,
    size: str = DEFAULT_SIZE,
    block_size: str = DEFAULT_BLOCK_SIZE,
    runtime: int = DEFAULT_RUNTIME,
    timeout: float = 90,
    warn_mbps: float = DEFAULT_WARN_MBPS,
    fail_mbps: float = DEFAULT_FAIL_MBPS,
    warn_ms: float = DEFAULT_WARN_MS,
    fail_ms: float = DEFAULT_FAIL_MS,
    elbencho_path: str = "elbencho",
    threads: int | None = None,
) -> tuple[PerfResult, PerfResult]:
    """Run one direct-I/O `elbencho` read or write pass against `target`'s
    OST (single-striped onto it), returning `(throughput_result,
    latency_result)` derived from that single run.
    """
    threads = threads or detect_cpu_thread_count()
    idx = ost_index(target)
    if idx is None:
        return _fail_pair(
            target.name, mode, f"could not determine OST index from name {target.name!r}"
        )

    path = os.path.join(mount_path, f".storage_validator_perf_{target.name}")
    try:
        setstripe = shell.run_cmd(
            ["lfs", "setstripe", "-i", str(idx), "-c", "1", path], timeout=timeout
        )
        if not setstripe.ok:
            return _fail_pair(target.name, mode, f"lfs setstripe failed: {setstripe.stderr.strip()}")

        rate, lat_us = _run_elbencho_rw(
            [path], mode, threads, size, block_size, runtime, elbencho_path, timeout
        )
    except (shell.CommandError, ElbenchoError) as exc:
        return _fail_pair(target.name, mode, f"elbencho failed: {exc}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    latency_ms = lat_us / 1000.0
    t_status, t_msg = _classify_throughput(rate, warn_mbps, fail_mbps)
    l_status, l_msg = _classify_latency(latency_ms, warn_ms, fail_ms)
    return (
        PerfResult(target=target.name, kind="throughput", value=rate, unit="MB/s",
                    status=t_status, message=t_msg, io_mode=mode),
        PerfResult(target=target.name, kind="latency", value=latency_ms, unit="ms",
                    status=l_status, message=l_msg, io_mode=mode),
    )


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


def _pool_ost_paths(pool_label: str, indices: list[int], mount_path: str, suffix: str) -> list[str]:
    """One dedicated scratch file per OST index, so each can be independently
    single-striped onto its own OST before the shared elbencho run.
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


def pool_rw_check(
    pool_label: str,
    targets: list[Target],
    mount_path: str,
    mode: IoMode,
    size: str = DEFAULT_SIZE,
    block_size: str = DEFAULT_BLOCK_SIZE,
    runtime: int = DEFAULT_RUNTIME,
    timeout: float = 90,
    warn_mbps: float = DEFAULT_WARN_MBPS,
    fail_mbps: float = DEFAULT_FAIL_MBPS,
    warn_ms: float = DEFAULT_WARN_MS,
    fail_ms: float = DEFAULT_FAIL_MS,
    elbencho_path: str = "elbencho",
    threads: int | None = None,
) -> tuple[PerfResult, PerfResult]:
    """Run one direct-I/O `elbencho` read or write pass across every OST in a
    pool at once (one single-striped scratch file per OST, `threads` worker
    threads total spread across all of them -- not multiplied by OST count),
    returning `(throughput_result, latency_result)` derived from that single
    run's aggregate/worst-case CSV output.
    """
    pool_kwargs = dict(pool=pool_label if pool_label != "(unpooled)" else None, scope="pool")
    indices = _group_ost_indices(targets)
    if not indices:
        return _fail_pair(
            f"pool:{pool_label}", mode, "could not determine any OST indices in pool", **pool_kwargs
        )

    threads = threads or detect_cpu_thread_count()
    stripe_count = len(indices)
    paths = _pool_ost_paths(pool_label, indices, mount_path, mode)
    try:
        err = _setstripe_per_ost(indices, paths, timeout)
        if err:
            return _fail_pair(f"pool:{pool_label}", mode, err, **pool_kwargs)

        rate, lat_us = _run_elbencho_rw(
            paths, mode, threads, size, block_size, runtime, elbencho_path, timeout
        )
    except (shell.CommandError, ElbenchoError) as exc:
        return _fail_pair(f"pool:{pool_label}", mode, f"elbencho failed: {exc}", **pool_kwargs)
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    latency_ms = lat_us / 1000.0
    detail = f"{stripe_count} OSTs, {threads} threads total"
    t_status, t_msg = _classify_throughput(rate, warn_mbps, fail_mbps)
    l_status, l_msg = _classify_latency(latency_ms, warn_ms, fail_ms)
    return (
        PerfResult(target=f"pool:{pool_label}", kind="throughput", value=rate, unit="MB/s",
                    status=t_status, message=f"{t_msg} ({detail})", io_mode=mode, **pool_kwargs),
        PerfResult(target=f"pool:{pool_label}", kind="latency", value=latency_ms, unit="ms",
                    status=l_status, message=f"{l_msg} ({detail})", io_mode=mode, **pool_kwargs),
    )


def run_perf_checks(
    topology: Topology,
    mount_path: str,
    size: str = DEFAULT_SIZE,
    block_size: str = DEFAULT_BLOCK_SIZE,
    runtime: int = DEFAULT_RUNTIME,
    timeout: float = 90,
    default_thresholds: PerfThresholds | None = None,
    pool_thresholds: dict[str, PerfThresholds] | None = None,
    elbencho_path: str = "elbencho",
    threads: int | None = None,
) -> list[PerfResult]:
    """Run write + read throughput/latency checks against every OST in the
    topology, then one write + read aggregate throughput/latency test per
    OST pool (plus one for any OSTs that aren't in a pool).

    Every single elbencho invocation (per-OST or per-pool) uses `threads`
    worker threads in total -- never multiplied by the number of OSTs in a
    pool -- so a run never exceeds the host's thread count (detected once
    via `lscpu`, or the `threads` override) regardless of topology size.
    Each pool's own thresholds are used so different drive types (e.g. ssd
    vs hdd pools) aren't judged against the same bar.
    """
    default_thresholds = default_thresholds or PerfThresholds()
    threads = threads or detect_cpu_thread_count()
    results: list[PerfResult] = []
    groups: dict[str, list[Target]] = defaultdict(list)
    for target in topology.osts:
        th = resolve_thresholds(target, default_thresholds, pool_thresholds)
        for mode in ("write", "read"):
            t_result, l_result = ost_rw_check(
                target, mount_path, mode, size, block_size, runtime, timeout,
                warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
                warn_ms=th.warn_ms, fail_ms=th.fail_ms,
                elbencho_path=elbencho_path, threads=threads,
            )
            t_result.pool = target.pool
            l_result.pool = target.pool
            results.append(t_result)
            results.append(l_result)
        pool_label = target.pool or "(unpooled)"
        groups[pool_label].append(target)

    for pool_label, group_targets in groups.items():
        th = (pool_thresholds or {}).get(pool_label, default_thresholds)
        for mode in ("write", "read"):
            t_result, l_result = pool_rw_check(
                pool_label, group_targets, mount_path, mode, size, block_size, runtime, timeout,
                warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
                warn_ms=th.warn_ms, fail_ms=th.fail_ms,
                elbencho_path=elbencho_path, threads=threads,
            )
            results.append(t_result)
            results.append(l_result)
    return results
