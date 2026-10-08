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


def run_perf_checks(
    topology: Topology,
    mount_path: str,
    size_mb: int = DEFAULT_SIZE_MB,
    timeout: float = 60,
) -> list[PerfResult]:
    """Run throughput + latency checks against every OST in the topology."""
    results: list[PerfResult] = []
    for target in topology.osts:
        results.append(throughput_check(target, mount_path, size_mb, timeout))
        results.append(latency_check(target, mount_path, timeout=timeout))
    return results
