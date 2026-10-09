"""Abstract storage backend interface and registry.

Backends (e.g. Lustre, future GPFS) implement discovery, health checks, and
perf checks behind this interface so the CLI/engine/reporting code never
depends on a specific filesystem implementation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from storage_validator.config import PerfConfig
    from storage_validator.models import CheckResult, PerfResult, Topology


class StorageBackend(ABC):
    """Interface implemented by each supported storage filesystem backend."""

    name: str

    @abstractmethod
    def discover(self) -> "Topology":
        """Discover filesystem topology (targets, pools, mounts)."""
        raise NotImplementedError

    @abstractmethod
    def run_health_checks(self, topology: "Topology") -> list["CheckResult"]:
        """Run health checks against the discovered topology."""
        raise NotImplementedError

    @abstractmethod
    def run_perf_checks(
        self,
        topology: "Topology",
        cfg: "PerfConfig",
        on_result: "Callable[[list[PerfResult]], None] | None" = None,
    ) -> list["PerfResult"]:
        """Run basic throughput/latency perf checks.

        If `on_result` is given, it's called with each check's results as
        soon as that check completes, so callers can stream results instead
        of waiting for the full run to finish.
        """
        raise NotImplementedError


BACKEND_REGISTRY: dict[str, type[StorageBackend]] = {}


def register_backend(name: str):
    """Class decorator registering a StorageBackend subclass under `name`."""

    def _decorator(cls: type[StorageBackend]) -> type[StorageBackend]:
        cls.name = name
        BACKEND_REGISTRY[name] = cls
        return cls

    return _decorator


def get_backend(name: str) -> type[StorageBackend]:
    try:
        return BACKEND_REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(sorted(BACKEND_REGISTRY)) or "<none registered>"
        raise ValueError(
            f"Unknown backend {name!r}. Available backends: {available}"
        ) from exc
