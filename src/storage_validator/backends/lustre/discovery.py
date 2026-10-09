"""Lustre topology discovery via `lfs`/`lctl` text parsing.

Parsers are defensive: unmatched/garbage lines are skipped (with a warning)
rather than raising, since `lctl`/`lfs` output format varies across Lustre
versions and deployments.
"""

from __future__ import annotations

import logging
import re

from storage_validator.models import Pool, Target, Topology

from . import shell

log = logging.getLogger(__name__)

# e.g. "  5 UP obdfilter scratch-OST0000 scratch-OST0000_UUID 7"
# (seen when running on a Lustre server / MDS/OSS node)
_DL_LINE_RE = re.compile(
    r"^\s*\d+\s+(?P<state>\S+)\s+(?P<type>mdt|obdfilter)\s+"
    r"(?P<name>\S+)\s+(?P<uuid>\S+)\s+\d+\s*$"
)

# e.g. "  4 UP mdc x3e09-MDT0003-mdc-ff3b05dfcf9f9800 9d0db091-... 4"
#      "  9 UP osc x3e09-OST0003-osc-ff3b05dfcf9f9800 9d0db091-... 4"
# (seen when running on a Lustre client node; the uuid column here is the
# *client's* connection uuid, shared across all devices, not the target's
# own uuid, so we derive a target uuid from its name instead.)
_DL_CLIENT_LINE_RE = re.compile(
    r"^\s*\d+\s+(?P<state>\S+)\s+(?P<type>mdc|osc)\s+"
    r"(?P<devname>\S+)\s+\S+\s+\d+\s*$"
)

# e.g. "  1 UP lov x3e09-clilov-ff3b05dfcf9f9800 ... 3"
#      "  2 UP lmv x3e09-clilmv-ff3b05dfcf9f9800 ... 4"
_DL_CLIENT_FSNAME_RE = re.compile(
    r"^\s*\d+\s+\S+\s+(?:lov|lmv)\s+(?P<name>\S+)\s+\S+\s+\d+\s*$"
)

# Known `lctl dl` device types that aren't MDTs/OSTs (or their client-side
# mdc/osc/lov/lmv counterparts) and so carry no target info for us to parse
# -- e.g. "  0 UP mgc MGC172.16.130.90@o2ib ... 4" (the management client
# connection). Lines with these types are silently skipped in
# `discover_targets` instead of logging a "skipping unparsed" warning, since
# they're recognized/expected, not garbage.
_DL_KNOWN_NON_TARGET_RE = re.compile(
    r"^\s*\d+\s+\S+\s+(?:mgc|mgs|lov|lmv)\s+\S+\s+\S+\s+\d+\s*$"
)

# e.g. "0: scratch-OST0000_UUID ACTIVE"
_OSTS_LINE_RE = re.compile(r"^\d+:\s+(?P<uuid>\S+)\s+(?P<state>\S+)\s*$")

# e.g. "scratch-OST0001_UUID       500.0G      475.0G       10.0G  95% ..."
_DF_LINE_RE = re.compile(
    r"^(?P<uuid>\S+)\s+(?P<bytes>\S+)\s+(?P<used>\S+)\s+"
    r"(?P<avail>\S+)\s+(?P<use_pct>\d+)%\s+(?P<mount>\S+)\s*$"
)

_POOL_LINE_RE = re.compile(r"^(?P<fsname>[\w-]+)\.(?P<pool>[\w-]+)\s*$")


def discover_fsname(timeout: float = 30) -> str:
    """Determine the Lustre filesystem name.

    Tries the `mdt.*.fsname` param first (only available on MDS/server
    nodes), then falls back to deriving it from the client-side `lov`/`lmv`
    device name reported by `lctl dl` (e.g. "x3e09-clilov-..." -> "x3e09").
    """
    result = shell.run_cmd(
        ["lctl", "get_param", "-n", "mdt.*.fsname"], timeout=timeout
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return line

    dl_result = shell.run_cmd(["lctl", "dl"], timeout=timeout)
    for line in dl_result.stdout.splitlines():
        match = _DL_CLIENT_FSNAME_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        for sep in ("-clilov-", "-clilmv-"):
            if sep in name:
                return name.split(sep)[0]

    log.warning("could not determine fsname from lctl get_param output")
    return "unknown"


def discover_targets(timeout: float = 30) -> list[Target]:
    """Parse `lctl dl` output into MDT/OST Target objects.

    Supports both server-side device lines (`mdt`/`obdfilter`, seen on
    MDS/OSS nodes) and client-side device lines (`mdc`/`osc`, seen on
    client nodes).
    """
    result = shell.run_cmd(["lctl", "dl"], timeout=timeout)
    targets_by_name: dict[str, Target] = {}
    for line in result.stdout.splitlines():
        match = _DL_LINE_RE.match(line)
        if match:
            # Server-side device line (mdt/obdfilter): authoritative, always
            # wins over any client-side (mdc/osc) line for the same target
            # (e.g. an MDS also has `osc` connections to remote OSTs).
            type_ = match.group("type")
            kind = "mdt" if type_ == "mdt" else "ost"
            name = match.group("name")
            targets_by_name[name] = Target(
                name=name,
                kind=kind,
                uuid=match.group("uuid"),
                state=match.group("state"),
            )
            continue
        match = _DL_CLIENT_LINE_RE.match(line)
        if match:
            type_ = match.group("type")
            kind = "mdt" if type_ == "mdc" else "ost"
            name = match.group("devname").split(f"-{type_}-")[0]
            if name not in targets_by_name:
                targets_by_name[name] = Target(
                    name=name,
                    kind=kind,
                    uuid=f"{name}_UUID",
                    state=match.group("state"),
                )
            continue
        if _DL_KNOWN_NON_TARGET_RE.match(line):
            continue
        if line.strip():
            log.warning("skipping unparsed `lctl dl` line: %r", line)
    return list(targets_by_name.values())


def discover_ost_activation(timeout: float = 30) -> dict[str, str]:
    """Parse `lfs osts` ACTIVE/INACTIVE state per OST UUID."""
    result = shell.run_cmd(["lfs", "osts"], timeout=timeout)
    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _OSTS_LINE_RE.match(line)
        if not match:
            continue
        states[match.group("uuid")] = match.group("state")
    return states


def discover_pools(fsname: str, timeout: float = 30) -> list[Pool]:
    """Parse `lfs pool_list <fsname>` and membership of each pool."""
    result = shell.run_cmd(["lfs", "pool_list", fsname], timeout=timeout)
    pools: list[Pool] = []
    for line in result.stdout.splitlines():
        match = _POOL_LINE_RE.match(line.strip())
        if not match or match.group("fsname") != fsname:
            continue
        pool_name = match.group("pool")
        members = discover_pool_members(fsname, pool_name, timeout=timeout)
        pools.append(Pool(name=pool_name, osts=members))
    return pools


def discover_pool_members(
    fsname: str, pool_name: str, timeout: float = 30
) -> list[str]:
    """Parse `lfs pool_list <fsname>.<pool>` OST membership."""
    result = shell.run_cmd(
        ["lfs", "pool_list", f"{fsname}.{pool_name}"], timeout=timeout
    )
    members: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("pool:"):
            continue
        members.append(line)
    return members


def discover_mounts(timeout: float = 30) -> list[str]:
    """Parse /proc/mounts for lustre client mount points."""
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        log.warning("could not read /proc/mounts: %s", exc)
        return []
    mounts = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "lustre":
            mounts.append(parts[1])
    return mounts


def discover_df(timeout: float = 30) -> dict[str, dict]:
    """Parse `lfs df -h` per-target usage info, keyed by target UUID."""
    result = shell.run_cmd(["lfs", "df", "-h"], timeout=timeout)
    df: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        match = _DF_LINE_RE.match(line.strip())
        if not match or match.group("uuid") == "filesystem_summary:":
            continue
        df[match.group("uuid")] = {
            "bytes": match.group("bytes"),
            "used": match.group("used"),
            "available": match.group("avail"),
            "use_pct": int(match.group("use_pct")),
            "mount": match.group("mount"),
        }
    return df


def discover_topology(timeout: float = 30) -> Topology:
    """Compose all discovery calls into a single Topology."""
    fsname = discover_fsname(timeout=timeout)
    targets = discover_targets(timeout=timeout)
    ost_states = discover_ost_activation(timeout=timeout)
    for uuid, state in ost_states.items():
        for i, target in enumerate(targets):
            if target.uuid == uuid and target.state is None:
                targets[i] = Target(
                    name=target.name,
                    kind=target.kind,
                    server=target.server,
                    device=target.device,
                    uuid=target.uuid,
                    state=state,
                )
    pools = discover_pools(fsname, timeout=timeout)
    uuid_to_pool: dict[str, str] = {}
    for pool in pools:
        for uuid in pool.osts:
            uuid_to_pool[uuid] = pool.name
    for i, target in enumerate(targets):
        pool_name = uuid_to_pool.get(target.uuid)
        if pool_name is not None:
            targets[i] = Target(
                name=target.name,
                kind=target.kind,
                server=target.server,
                device=target.device,
                uuid=target.uuid,
                state=target.state,
                pool=pool_name,
            )
    mdts = [t for t in targets if t.kind == "mdt"]
    osts = [t for t in targets if t.kind == "ost"]
    mounts = discover_mounts(timeout=timeout)
    df = discover_df(timeout=timeout)
    return Topology(
        fsname=fsname, mdts=mdts, osts=osts, pools=pools, mounts=mounts, df=df
    )
