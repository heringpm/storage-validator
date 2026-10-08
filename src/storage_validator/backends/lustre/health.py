"""Health checks evaluated against a discovered Lustre Topology.

Each check is a pure function of `Topology` (plus optional thresholds) that
returns a list of `CheckResult`. Keeping them pure/side-effect-free (no
subprocess calls) makes them trivial to unit test with hand-built Topology
fixtures.
"""

from __future__ import annotations

from storage_validator.models import CheckResult, Topology

DEFAULT_WARN_PCT = 80
DEFAULT_FAIL_PCT = 95

# Lustre device states as reported by `lctl dl`. "UP" is healthy; anything
# else (IN, ST, etc.) indicates the target is not fully operational.
_HEALTHY_STATE = "UP"


def check_target_states(topology: Topology) -> list[CheckResult]:
    """Flag any MDT/OST whose `lctl dl` state is not UP."""
    results: list[CheckResult] = []
    for target in [*topology.mdts, *topology.osts]:
        if target.state == _HEALTHY_STATE:
            results.append(
                CheckResult(
                    name="target_state",
                    status="PASS",
                    message=f"{target.name} is UP",
                    target=target.name,
                )
            )
        else:
            results.append(
                CheckResult(
                    name="target_state",
                    status="FAIL",
                    message=f"{target.name} state is {target.state!r}, expected UP",
                    target=target.name,
                    details={"state": target.state},
                )
            )
    return results


def check_usage_thresholds(
    topology: Topology,
    warn_pct: int = DEFAULT_WARN_PCT,
    fail_pct: int = DEFAULT_FAIL_PCT,
) -> list[CheckResult]:
    """Flag targets whose `lfs df` usage crosses warn/fail thresholds."""
    results: list[CheckResult] = []
    uuid_to_name = {t.uuid: t.name for t in [*topology.mdts, *topology.osts]}
    for uuid, info in topology.df.items():
        use_pct = info.get("use_pct")
        if use_pct is None:
            continue
        name = uuid_to_name.get(uuid, uuid)
        if use_pct >= fail_pct:
            status, message = (
                "FAIL",
                f"{name} usage {use_pct}% >= fail threshold {fail_pct}%",
            )
        elif use_pct >= warn_pct:
            status, message = (
                "WARN",
                f"{name} usage {use_pct}% >= warn threshold {warn_pct}%",
            )
        else:
            status, message = "PASS", f"{name} usage {use_pct}% OK"
        results.append(
            CheckResult(
                name="usage_threshold",
                status=status,
                message=message,
                target=name,
                details={"use_pct": use_pct},
            )
        )
    return results


def check_pool_membership(topology: Topology) -> list[CheckResult]:
    """Flag pools that reference OSTs not present in discovered topology."""
    results: list[CheckResult] = []
    known_ost_uuids = {t.uuid for t in topology.osts}
    for pool in topology.pools:
        unknown = [uuid for uuid in pool.osts if uuid not in known_ost_uuids]
        if unknown:
            results.append(
                CheckResult(
                    name="pool_membership",
                    status="WARN",
                    message=f"pool {pool.name} references unknown OSTs: {unknown}",
                    target=pool.name,
                    details={"unknown_osts": unknown},
                )
            )
        else:
            results.append(
                CheckResult(
                    name="pool_membership",
                    status="PASS",
                    message=f"pool {pool.name} membership OK",
                    target=pool.name,
                )
            )
    return results


def check_client_mounts(topology: Topology) -> list[CheckResult]:
    """Flag the absence of any local Lustre client mount."""
    if topology.mounts:
        return [
            CheckResult(
                name="client_mount",
                status="PASS",
                message=f"found {len(topology.mounts)} lustre mount(s)",
                details={"mounts": list(topology.mounts)},
            )
        ]
    return [
        CheckResult(
            name="client_mount",
            status="WARN",
            message="no local lustre client mounts found",
        )
    ]


def run_health_checks(
    topology: Topology,
    warn_pct: int = DEFAULT_WARN_PCT,
    fail_pct: int = DEFAULT_FAIL_PCT,
) -> list[CheckResult]:
    """Run all health checks and return the combined list of results."""
    results: list[CheckResult] = []
    results.extend(check_target_states(topology))
    results.extend(check_usage_thresholds(topology, warn_pct, fail_pct))
    results.extend(check_pool_membership(topology))
    results.extend(check_client_mounts(topology))
    return results
