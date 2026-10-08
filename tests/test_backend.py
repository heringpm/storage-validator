from unittest.mock import patch

from storage_validator.backends import lustre  # noqa: F401  (triggers registration)
from storage_validator.backends.base import BACKEND_REGISTRY, get_backend
from storage_validator.backends.lustre.backend import LustreBackend
from storage_validator.config import PerfConfig
from storage_validator.models import PerfResult, Target, Topology


def test_lustre_backend_registered():
    assert BACKEND_REGISTRY["lustre"] is LustreBackend
    assert get_backend("lustre") is LustreBackend


def test_run_health_checks_uses_configured_thresholds():
    backend = LustreBackend()
    topo = Topology(
        fsname="scratch",
        osts=[Target(name="scratch-OST0000", kind="ost", uuid="u0", state="UP")],
        df={"u0": {"use_pct": 90}},
    )
    results = backend.run_health_checks(topo)
    usage_results = [r for r in results if r.name == "usage_threshold"]
    assert usage_results[0].status == "WARN"


def test_run_perf_checks_no_mount_path_fails_gracefully():
    backend = LustreBackend()
    topo = Topology(fsname="scratch", mounts=[])
    results = backend.run_perf_checks(topo, PerfConfig(mount_path=None))
    assert len(results) == 1
    assert results[0].status == "FAIL"


def test_run_perf_checks_falls_back_to_topology_mount():
    backend = LustreBackend()
    topo = Topology(
        fsname="scratch",
        osts=[Target(name="scratch-OST0000", kind="ost", uuid="u0")],
        mounts=["/mnt/scratch"],
    )
    with patch(
        "storage_validator.backends.lustre.perf.run_perf_checks",
        return_value=[PerfResult(target="x", kind="throughput", value=1.0, unit="MB/s", status="PASS")],
    ) as mock_run:
        results = backend.run_perf_checks(topo, PerfConfig())
    mock_run.assert_called_once()
    assert mock_run.call_args[0][1] == "/mnt/scratch"
    assert len(results) == 1
