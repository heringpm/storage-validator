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
class PerfConfig:
    mount_path: Optional[str] = None
    size_mb: int = 256
    timeout: float = 60
    warn_mbps: float = 200.0
    fail_mbps: float = 50.0
    warn_ms: float = 10.0
    fail_ms: float = 50.0


@dataclass
class Config:
    backend: str = "lustre"
    skip_perf: bool = False
    health: HealthConfig = field(default_factory=HealthConfig)
    perf: PerfConfig = field(default_factory=PerfConfig)
    output_json: Optional[str] = None
