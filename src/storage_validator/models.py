"""Data models shared across backends, engine, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

TargetKind = Literal["mdt", "ost"]
CheckStatus = Literal["PASS", "WARN", "FAIL"]
PerfKind = Literal["throughput", "latency"]


@dataclass(frozen=True)
class Target:
    name: str
    kind: TargetKind
    server: Optional[str] = None
    device: Optional[str] = None
    uuid: Optional[str] = None
    state: Optional[str] = None
    pool: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "server": self.server,
            "device": self.device,
            "uuid": self.uuid,
            "state": self.state,
            "pool": self.pool,
        }


@dataclass(frozen=True)
class Pool:
    name: str
    osts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "osts": list(self.osts)}


@dataclass
class Topology:
    fsname: str
    mdts: list[Target] = field(default_factory=list)
    osts: list[Target] = field(default_factory=list)
    pools: list[Pool] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    df: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fsname": self.fsname,
            "mdts": [t.to_dict() for t in self.mdts],
            "osts": [t.to_dict() for t in self.osts],
            "pools": [p.to_dict() for p in self.pools],
            "mounts": list(self.mounts),
            "df": dict(self.df),
        }


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    target: Optional[str] = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass
class PerfResult:
    target: str
    kind: PerfKind
    value: float
    unit: str
    status: CheckStatus
    message: str = ""
    pool: Optional[str] = None
    scope: Literal["ost", "pool"] = "ost"

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "message": self.message,
            "pool": self.pool,
            "scope": self.scope,
        }


@dataclass
class Report:
    fsname: str
    timestamp: str
    topology: Topology
    health: list[CheckResult] = field(default_factory=list)
    perf: list[PerfResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fsname": self.fsname,
            "timestamp": self.timestamp,
            "topology": self.topology.to_dict(),
            "health": [c.to_dict() for c in self.health],
            "perf": [p.to_dict() for p in self.perf],
        }

    def overall_status(self) -> CheckStatus:
        statuses = {c.status for c in self.health} | {p.status for p in self.perf}
        if "FAIL" in statuses:
            return "FAIL"
        if "WARN" in statuses:
            return "WARN"
        return "PASS"
