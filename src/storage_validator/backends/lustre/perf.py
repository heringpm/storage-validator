"""Per-OST and per-pool read/write throughput+latency perf checks.

Both per-OST checks (`ost_rw_check`) and per-pool checks (`pool_rw_check`)
measure performance using `elbencho`, a multi-threaded direct-I/O benchmark
tool. Each check runs exactly one write pass followed by one read pass
against the same scratch file(s), deriving throughput ("MiB/s [last]") and
latency ("IO lat us [max]") from each pass's CSV output -- there is no
separate latency-only test, and the write pass's data is reused for the
read pass instead of writing it twice.

Scratch files live under a persistent `.storageval/` directory tree at the
root of the mount, laid out as:

    <mount>/.storageval/<pool-or-fsname>/<OST name>/   -- per-OST scratch dir
    <mount>/.storageval/<pool-or-fsname>/pool/         -- per-pool scratch dir

Each of these directories is single-`lfs setstripe`'d *once*, as a
directory, rather than striping every scratch file individually: an OST's
directory is striped onto just that one OST, and a pool's directory is
striped across the whole pool (`-p <pool> -c -1`, or `-c -1` for the
unpooled default). Every file created inside a directory automatically
inherits its layout, so individual scratch files need no per-file
`setstripe` call -- and the pool directory gets Lustre's own real
round-robin allocation across the pool's OSTs instead of us manually
assigning files to OSTs. If a target has no pool, its directory lives under
the filesystem name instead (there's no real pool to scope it to).

Every worker thread gets its own dedicated scratch file inside the relevant
directory, created and named automatically by elbencho itself (`--dirs 0
--files 1` against the directory path, rather than us listing out one
explicit file path per thread on the command line). A check always uses
exactly `threads` files/threads in total -- never multiplied by the number
of OSTs in a pool -- so a run on a single system never uses more worker
threads than the configured/detected CPU thread count.

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
from typing import Callable, Literal

from storage_validator.config import PerfThresholds
from storage_validator.models import PerfResult, Target, Topology

from . import shell

log = logging.getLogger(__name__)

IoMode = Literal["read", "write"]

DEFAULT_SIZE = "10g"
DEFAULT_BLOCK_SIZE = "1m"
DEFAULT_RUNTIME = 60
DEFAULT_WARN_MBPS = 200.0
DEFAULT_FAIL_MBPS = 50.0
DEFAULT_WARN_MS = 10.0
DEFAULT_FAIL_MS = 50.0

_OST_INDEX_RE = re.compile(r"OST([0-9a-fA-F]+)$")

STORAGEVAL_DIR = ".storageval"


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


def _pool_dir_label(pool: str | None, fsname: str) -> str:
    """Directory name under `.storageval/` for a pool: the real pool name,
    or the filesystem name if there's no real pool to scope it to.
    """
    return pool or fsname


def ost_scratch_dir(mount_path: str, fsname: str, target: Target) -> str:
    """Persistent per-OST scratch directory, single-striped onto just that
    OST so files created inside it inherit the layout automatically.
    """
    return os.path.join(
        mount_path, STORAGEVAL_DIR, _pool_dir_label(target.pool, fsname), target.name
    )


def pool_scratch_dir(mount_path: str, fsname: str, pool_label: str) -> str:
    """Persistent per-pool scratch directory, striped across the whole pool
    (or the unpooled default) so files created inside it are spread across
    every OST by Lustre's own allocator.
    """
    dir_label = fsname if pool_label == "(unpooled)" else pool_label
    return os.path.join(mount_path, STORAGEVAL_DIR, dir_label, "pool")


def _ensure_striped_dir(path: str, stripe_args: list[str], timeout: float) -> str | None:
    """Create `path` (and parents) if missing, then apply `lfs setstripe
    <stripe_args> path` so every file later created inside it inherits that
    layout -- no per-file `setstripe` call needed.

    Idempotent: re-applying the same layout to an already-striped directory
    is a no-op, so this is safe to call on every run, reusing the directory
    (and its layout) across runs instead of recreating it each time.

    In dry-run mode, prints the `mkdir`/`lfs setstripe` commands that would
    be run instead of actually creating/striping anything.

    Returns an error message on failure, else None.
    """
    mkdir_cmd = ["mkdir", "-p", path]
    setstripe_cmd = ["lfs", "setstripe", *stripe_args, path]
    if shell.DRY_RUN:
        shell.print_dry_run(mkdir_cmd)
        shell.print_dry_run(setstripe_cmd)
        return None
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return f"mkdir failed for {path}: {exc}"
    result = shell.run_cmd(setstripe_cmd, timeout=timeout)
    if not result.ok:
        return f"lfs setstripe failed for {path}: {result.stderr.strip()}"
    return None


_DAEMON_LOG_PATH = "/tmp/storage_validator_elbencho_daemon.log"


def start_elbencho_daemons(
    hosts: list[str], elbencho_path: str = "elbencho", timeout: float = 30
) -> list[str]:
    """SSH to each of `hosts` and start a detached `elbencho --daemon`
    process (logging to `_DAEMON_LOG_PATH` on that host), so a later
    `elbencho --hosts h1,h2,...` invocation can coordinate a distributed
    benchmark across all of them.

    Returns the list of hosts that failed to start (empty if all succeeded).
    In dry-run mode, prints the `ssh` commands instead of running them and
    returns an empty list.
    """
    failed = []
    for host in hosts:
        remote_cmd = (
            f"nohup {elbencho_path} --daemon > {_DAEMON_LOG_PATH} 2>&1 < /dev/null &"
        )
        cmd = ["ssh", "-f", host, remote_cmd]
        if shell.DRY_RUN:
            shell.print_dry_run(cmd)
            continue
        try:
            result = shell.run_cmd(cmd, timeout=timeout)
        except shell.CommandError:
            failed.append(host)
            continue
        if not result.ok:
            failed.append(host)
    return failed


def stop_elbencho_daemons(
    hosts: list[str], timeout: float = 30
) -> None:
    """SSH to each of `hosts` and kill its `elbencho --daemon` process.

    Best-effort: a host with no running daemon (or an unreachable host)
    doesn't raise -- this is cleanup, not a check that must pass. No-op in
    dry-run mode other than printing the commands.
    """
    for host in hosts:
        cmd = ["ssh", host, "pkill", "-f", "elbencho --daemon"]
        if shell.DRY_RUN:
            shell.print_dry_run(cmd)
            continue
        try:
            shell.run_cmd(cmd, timeout=timeout)
        except shell.CommandError:
            pass


def _clear_scratch_dir(path: str) -> None:
    """Remove every regular file directly inside `path` (the scratch files
    elbencho created, one per worker thread), leaving the directory itself
    (and its striping) in place for reuse on the next run.

    No-op in dry-run mode, since nothing was actually written.
    """
    if shell.DRY_RUN:
        return
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        entry_path = os.path.join(path, name)
        try:
            if os.path.isfile(entry_path):
                os.remove(entry_path)
        except OSError:
            pass


def _read_elbencho_csv_last_row(csv_path: str, operation: str) -> dict[str, str] | None:
    """Read the row elbencho wrote to its `--csvfile` output for `operation`
    ("WRITE" or "READ").

    When `--sync` is passed (write pass only), elbencho appends a *second*
    row for the separate SYNC phase after the WRITE row, with all
    throughput/latency fields blank -- so we can't just take the last row,
    we have to pick the row whose `operation` column matches the phase we
    actually care about.
    """
    try:
        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    for row in reversed(rows):
        if row.get("operation") == operation:
            return row
    return rows[-1] if rows else None


def _run_elbencho_rw(
    directory: str,
    mode: IoMode,
    threads: int,
    size: str,
    block_size: str,
    runtime: int,
    elbencho_path: str,
    timeout: float,
    hosts: list[str] | None = None,
) -> tuple[float, float]:
    """Run one elbencho read or write pass against `directory` with a total
    of `threads` worker threads per participating host, and return
    `(throughput_mibs, latency_us)` parsed from its CSV output.

    `--dirs 0 --files 1` tells elbencho to create exactly one file per
    thread directly inside `directory` (not in per-thread subdirs), with
    elbencho itself choosing/generating each file's name -- so we never have
    to list out one explicit file path per thread on the command line.

    If `hosts` is given, elbencho is run in distributed mode (`--hosts
    h1,h2,...`), coordinating an `elbencho --daemon` already running on each
    host (see `start_elbencho_daemons`) so the combined throughput of every
    host hitting the filesystem at once is measured, instead of just one
    client's local thread count.

    The write pass adds `--sync`, so elbencho fsyncs each file before
    exiting. Without this, `write()` under `--direct` can return (and our
    subprocess call can complete) before the data is actually durable on the
    OST backend, letting the immediately-following read pass's I/O overlap
    with the write's still-settling backend I/O.

    The write pass also adds `--trunctosize`, so each file is truncated (or
    extended) to exactly `size` via `ftruncate()` before writing. Without
    this, a leftover scratch file from a prior run that didn't finish
    writing a full-size file (e.g. it hit `--timelimit` partway through, or
    errored out) would still be sitting at its old, smaller size; elbencho
    refuses to write at an offset beyond a pre-existing file's current size
    under `--direct`, so the next run would fail with "Given offset plus
    size to use is larger than detected size." (Note: `--trunc` alone
    truncates to *0*, not to `size`, so it doesn't fix this.)

    Raises `ElbenchoError` if the command fails or its output can't be
    parsed, `shell.CommandError` if the command itself couldn't be run.
    """
    io_flag = "-w" if mode == "write" else "-r"
    extra_flags = ["--sync", "--trunctosize"] if mode == "write" else []
    csv_fd, csv_path = tempfile.mkstemp(prefix="storage_validator_elbencho_", suffix=".csv")
    os.close(csv_fd)
    os.remove(csv_path)
    host_flags = ["--hosts", ",".join(hosts)] if hosts else []
    cmd = [
        elbencho_path, io_flag, "-t", str(threads), "-b", block_size,
        "-s", size, "--direct", "--lat", "--timelimit", str(runtime),
        "--dirs", "0", "--files", "1", "--csvfile", csv_path,
    ] + host_flags + extra_flags + [directory]
    if shell.DRY_RUN:
        shell.print_dry_run(cmd)
        try:
            os.remove(csv_path)
        except OSError:
            pass
        return 0.0, 0.0
    try:
        result = shell.run_cmd(cmd, timeout=timeout)
        if not result.ok:
            raise ElbenchoError((result.stderr or result.stdout).strip())

        row = _read_elbencho_csv_last_row(csv_path, "WRITE" if mode == "write" else "READ")
        if row is None or not row.get("MiB/s [last]") or not row.get("IO lat us [max]"):
            raise ElbenchoError(
                "could not parse elbencho CSV output (missing --lat?); "
                f"parsed row: {row!r}"
            )
        try:
            return float(row["MiB/s [last]"]), float(row["IO lat us [max]"])
        except ValueError as exc:
            raise ElbenchoError(f"could not parse elbencho CSV output: {exc}") from exc
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


def _fail_quad(
    target_name: str, message: str,
    pool: str | None = None, scope: Literal["ost", "pool"] = "ost",
) -> tuple[PerfResult, PerfResult, PerfResult, PerfResult]:
    results = []
    for mode in ("write", "read"):
        common = dict(target=target_name, status="FAIL", message=message, pool=pool, scope=scope, io_mode=mode)
        results.append(PerfResult(kind="throughput", value=0.0, unit="MB/s", **common))
        results.append(PerfResult(kind="latency", value=0.0, unit="ms", **common))
    return tuple(results)


def _write_then_read(
    directory: str,
    threads: int,
    size: str,
    block_size: str,
    runtime: int,
    elbencho_path: str,
    timeout: float,
    hosts: list[str] | None = None,
) -> tuple[float, float, float, float]:
    """Run one write pass against `directory` (one file per worker thread,
    created by elbencho itself), then read that same data back, returning
    `(write_mibs, write_lat_us, read_mibs, read_lat_us)`.

    The write pass's data is reused for the read pass instead of writing it
    twice. If `hosts` is given, both passes run across every host at once
    (see `_run_elbencho_rw`).
    """
    write_rate, write_lat_us = _run_elbencho_rw(
        directory, "write", threads, size, block_size, runtime, elbencho_path, timeout, hosts
    )
    read_rate, read_lat_us = _run_elbencho_rw(
        directory, "read", threads, size, block_size, runtime, elbencho_path, timeout, hosts
    )
    return write_rate, write_lat_us, read_rate, read_lat_us


def _build_results(
    target_name: str,
    write_rate: float, write_lat_us: float, read_rate: float, read_lat_us: float,
    warn_mbps: float, fail_mbps: float, warn_ms: float, fail_ms: float,
    pool: str | None = None, scope: Literal["ost", "pool"] = "ost",
    detail: str = "",
) -> tuple[PerfResult, PerfResult, PerfResult, PerfResult]:
    results = []
    for mode, rate, lat_us in (("write", write_rate, write_lat_us), ("read", read_rate, read_lat_us)):
        latency_ms = lat_us / 1000.0
        t_status, t_msg = _classify_throughput(rate, warn_mbps, fail_mbps)
        l_status, l_msg = _classify_latency(latency_ms, warn_ms, fail_ms)
        if detail:
            t_msg, l_msg = f"{t_msg} ({detail})", f"{l_msg} ({detail})"
        common = dict(target=target_name, pool=pool, scope=scope, io_mode=mode)
        results.append(PerfResult(kind="throughput", value=rate, unit="MB/s", status=t_status, message=t_msg, **common))
        results.append(PerfResult(kind="latency", value=latency_ms, unit="ms", status=l_status, message=l_msg, **common))
    return tuple(results)


def ost_rw_check(
    target: Target,
    mount_path: str,
    fsname: str = "fs",
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
    hosts: list[str] | None = None,
) -> tuple[PerfResult, PerfResult, PerfResult, PerfResult]:
    """Run one direct-I/O `elbencho` write pass immediately followed by one
    read pass against `target`'s OST, returning
    `(write_throughput, write_latency, read_throughput, read_latency)`.

    All scratch files live inside `target`'s persistent scratch directory
    (see `ost_scratch_dir`), which is itself single-striped onto `target`'s
    OST -- so files created inside it inherit that layout automatically and
    need no per-file `setstripe` call. elbencho itself creates one scratch
    file per worker thread directly inside that directory (`--dirs 0
    --files 1`), so we never have to list individual file paths on the
    command line. The write pass's data is reused for the read pass, and the
    scratch files (but not the directory itself, which is reused across
    runs) are only removed once both passes have completed.

    If `hosts` is given, every host runs `threads` worker threads against
    the same shared `ost_dir` at once (see `_run_elbencho_rw`), measuring
    this OST's throughput under combined load from every client instead of
    just this one.
    """
    threads = threads or detect_cpu_thread_count()
    idx = ost_index(target)
    if idx is None:
        return _fail_quad(
            target.name, f"could not determine OST index from name {target.name!r}"
        )

    ost_dir = ost_scratch_dir(mount_path, fsname, target)
    err = _ensure_striped_dir(ost_dir, ["-i", str(idx), "-c", "1"], timeout)
    if err:
        return _fail_quad(target.name, err)

    try:
        write_rate, write_lat_us, read_rate, read_lat_us = _write_then_read(
            ost_dir, threads, size, block_size, runtime, elbencho_path, timeout, hosts
        )
    except (shell.CommandError, ElbenchoError) as exc:
        return _fail_quad(target.name, f"elbencho failed: {exc}")
    finally:
        _clear_scratch_dir(ost_dir)

    return _build_results(
        target.name, write_rate, write_lat_us, read_rate, read_lat_us,
        warn_mbps, fail_mbps, warn_ms, fail_ms,
        detail="dry run, no real data" if shell.DRY_RUN else "",
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


def pool_rw_check(
    pool_label: str,
    targets: list[Target],
    mount_path: str,
    fsname: str = "fs",
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
    hosts: list[str] | None = None,
) -> tuple[PerfResult, PerfResult, PerfResult, PerfResult]:
    """Run one direct-I/O `elbencho` write pass immediately followed by one
    read pass across every OST in a pool at once, returning
    `(write_throughput, write_latency, read_throughput, read_latency)`.

    All scratch files live inside the pool's persistent scratch directory
    (see `pool_scratch_dir`), which is itself striped across the whole pool
    (`lfs setstripe -p <pool> -c -1`, or `-c -1` for the unpooled default)
    -- so files created inside it get Lustre's own real round-robin
    allocation across every OST in the pool, instead of us manually
    assigning individual files to specific OSTs. This is what makes it a
    genuine pool-level perf test rather than a simulated one. Each of the
    `threads` worker threads gets its own dedicated scratch file in that
    directory. The write pass's data is reused for the read pass, and the
    scratch files (but not the directory itself, which is reused across
    runs) are only removed once both passes have completed.

    If `hosts` is given, every host runs `threads` worker threads against
    the shared `pool_dir` at once (see `_run_elbencho_rw`), measuring the
    pool's combined throughput under load from every client at once.
    """
    pool_kwargs = dict(pool=pool_label if pool_label != "(unpooled)" else None, scope="pool")
    indices = _group_ost_indices(targets)
    if not indices:
        return _fail_quad(
            f"pool:{pool_label}", "could not determine any OST indices in pool", **pool_kwargs
        )

    threads = threads or detect_cpu_thread_count()
    stripe_count = len(indices)
    pool_dir = pool_scratch_dir(mount_path, fsname, pool_label)
    stripe_args = (
        ["-p", pool_label, "-c", "-1"] if pool_label != "(unpooled)" else ["-c", "-1"]
    )
    err = _ensure_striped_dir(pool_dir, stripe_args, timeout)
    if err:
        return _fail_quad(f"pool:{pool_label}", err, **pool_kwargs)

    try:
        write_rate, write_lat_us, read_rate, read_lat_us = _write_then_read(
            pool_dir, threads, size, block_size, runtime, elbencho_path, timeout, hosts
        )
    except (shell.CommandError, ElbenchoError) as exc:
        return _fail_quad(f"pool:{pool_label}", f"elbencho failed: {exc}", **pool_kwargs)
    finally:
        _clear_scratch_dir(pool_dir)

    detail = f"{stripe_count} OSTs, {threads} threads total"
    if hosts:
        detail += f", {len(hosts)} hosts"
    if shell.DRY_RUN:
        detail += ", dry run, no real data"
    return _build_results(
        f"pool:{pool_label}", write_rate, write_lat_us, read_rate, read_lat_us,
        warn_mbps, fail_mbps, warn_ms, fail_ms, detail=detail, **pool_kwargs,
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
    ost_names: set[str] | None = None,
    pool_names: set[str] | None = None,
    hosts: list[str] | None = None,
    on_result: Callable[[tuple[PerfResult, PerfResult, PerfResult, PerfResult]], None] | None = None,
) -> list[PerfResult]:
    """Run one write+read throughput/latency check against every OST in the
    topology, then one write+read aggregate throughput/latency test per OST
    pool (plus one for any OSTs that aren't in a pool).

    Every single check (per-OST or per-pool) uses `threads` worker
    threads/files in total per participating host -- never multiplied by
    the number of OSTs in a pool -- so a run never exceeds the host's
    thread count (detected once via `lscpu`, or the `threads` override)
    regardless of topology size. Each pool's own thresholds are used so
    different drive types (e.g. ssd vs hdd pools) aren't judged against the
    same bar.

    `ost_names`, if given, restricts per-OST checks to only the named OSTs
    (matched against `Target.name`). `pool_names`, if given, restricts both
    per-pool checks to only the named pools *and* per-OST checks to only
    OSTs belonging to one of those pools (so `--pool flash` tests just the
    OSTs in the "flash" pool, not every OST in the filesystem). Passing
    only `ost_names` still runs per-pool checks for every pool, using all
    of that pool's OSTs, not just the named ones.

    If `hosts` is given (a list of hostnames/IPs reachable via passwordless
    SSH), an `elbencho --daemon` is started on each host before any checks
    run (and stopped again afterwards, even if a check fails), and every
    `elbencho` invocation runs in distributed mode across all of them at
    once (`--hosts h1,h2,...`) -- so each check measures combined
    throughput from every host hitting the filesystem simultaneously,
    instead of just the local client.

    If `on_result` is given, it's called with each check's 4-tuple of
    results (write throughput/latency, read throughput/latency) as soon as
    that check finishes, so a caller can stream results instead of waiting
    for every OST/pool to be checked before seeing anything.
    """
    default_thresholds = default_thresholds or PerfThresholds()
    threads = threads or detect_cpu_thread_count()
    results: list[PerfResult] = []

    if hosts:
        failed_hosts = start_elbencho_daemons(hosts, elbencho_path, timeout)
        if failed_hosts:
            log.warning(
                "failed to start elbencho daemon on: %s -- continuing without them",
                ", ".join(failed_hosts),
            )
            hosts = [h for h in hosts if h not in failed_hosts]
        if not hosts:
            hosts = None

    try:
        ost_targets = topology.osts
        if ost_names:
            ost_targets = [t for t in ost_targets if t.name in ost_names]
        if pool_names:
            ost_targets = [t for t in ost_targets if (t.pool or "(unpooled)") in pool_names]
        for target in ost_targets:
            th = resolve_thresholds(target, default_thresholds, pool_thresholds)
            quad = ost_rw_check(
                target, mount_path, topology.fsname, size, block_size, runtime, timeout,
                warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
                warn_ms=th.warn_ms, fail_ms=th.fail_ms,
                elbencho_path=elbencho_path, threads=threads, hosts=hosts,
            )
            for result in quad:
                result.pool = target.pool
                results.append(result)
            if on_result:
                on_result(quad)

        groups: dict[str, list[Target]] = defaultdict(list)
        for target in topology.osts:
            pool_label = target.pool or "(unpooled)"
            groups[pool_label].append(target)

        for pool_label, group_targets in groups.items():
            if pool_names and pool_label not in pool_names:
                continue
            th = (pool_thresholds or {}).get(pool_label, default_thresholds)
            quad = pool_rw_check(
                pool_label, group_targets, mount_path, topology.fsname, size, block_size, runtime, timeout,
                warn_mbps=th.warn_mbps, fail_mbps=th.fail_mbps,
                warn_ms=th.warn_ms, fail_ms=th.fail_ms,
                elbencho_path=elbencho_path, threads=threads, hosts=hosts,
            )
            results.extend(quad)
            if on_result:
                on_result(quad)
    finally:
        if hosts:
            stop_elbencho_daemons(hosts, timeout)

    return results
