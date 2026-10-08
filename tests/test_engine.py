from unittest.mock import patch

import pytest

from storage_validator import engine
from storage_validator.config import Config
from storage_validator.models import CheckResult, PerfResult, Topology


def _topology():
    return Topology(fsname="scratch")


def test_run_calls_discover_health_and_perf():
    topo = _topology()
    health_results = [CheckResult(name="x", status="PASS", message="ok")]
    perf_results = [PerfResult(target="t", kind="throughput", value=1.0, unit="MB/s", status="PASS")]

    with patch(
        "storage_validator.backends.lustre.backend.LustreBackend.discover",
        return_value=topo,
    ), patch(
        "storage_validator.backends.lustre.backend.LustreBackend.run_health_checks",
        return_value=health_results,
    ), patch(
        "storage_validator.backends.lustre.backend.LustreBackend.run_perf_checks",
        return_value=perf_results,
    ) as mock_perf:
        report = engine.run(Config())

    assert report.fsname == "scratch"
    assert report.health == health_results
    assert report.perf == perf_results
    mock_perf.assert_called_once()


def test_run_skips_perf_when_configured():
    topo = _topology()
    with patch(
        "storage_validator.backends.lustre.backend.LustreBackend.discover",
        return_value=topo,
    ), patch(
        "storage_validator.backends.lustre.backend.LustreBackend.run_health_checks",
        return_value=[],
    ), patch(
        "storage_validator.backends.lustre.backend.LustreBackend.run_perf_checks",
    ) as mock_perf:
        report = engine.run(Config(skip_perf=True))

    assert report.perf == []
    mock_perf.assert_not_called()


def test_run_unknown_backend_raises():
    with pytest.raises(ValueError):
        engine.run(Config(backend="nope"))
