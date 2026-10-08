from unittest.mock import patch

from storage_validator.backends.lustre import perf
from storage_validator.models import Target

OST0 = Target(name="scratch-OST0000", kind="ost", uuid="ost0_uuid")
BAD_NAME = Target(name="not-an-ost", kind="ost", uuid="x")


def test_ost_index():
    assert perf.ost_index(OST0) == 0
    assert perf.ost_index(Target(name="scratch-OST000a", kind="ost")) == 10
    assert perf.ost_index(BAD_NAME) is None


def test_parse_dd_rate_mbps():
    stderr = "1048576 bytes (1.0 MB, 1.0 MiB) copied, 0.0123 s, 85.2 MB/s"
    assert perf.parse_dd_rate_mbps(stderr) == 85.2


def test_parse_dd_rate_mbps_gbps_unit():
    stderr = "copied, 1.0 s, 1.5 GB/s"
    assert perf.parse_dd_rate_mbps(stderr) == 1536.0


def test_parse_dd_rate_mbps_unparsable():
    assert perf.parse_dd_rate_mbps("garbage output") is None


def _ok(stdout="", stderr=""):
    return shell_result(0, stdout, stderr)


def shell_result(returncode, stdout="", stderr=""):
    from storage_validator.backends.lustre.shell import CommandResult

    return CommandResult(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_throughput_check_pass(tmp_path):
    dd_stderr = "268435456 bytes copied, 1.0 s, 300 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)],
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "PASS"
    assert result.value == 300.0


def test_throughput_check_warn(tmp_path):
    dd_stderr = "268435456 bytes copied, 1.0 s, 100 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)],
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "WARN"


def test_throughput_check_fail_low_rate(tmp_path):
    dd_stderr = "268435456 bytes copied, 1.0 s, 10 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)],
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "FAIL"


def test_throughput_check_setstripe_failure(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        return_value=shell_result(1, "", "setstripe error"),
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "FAIL"
    assert "setstripe" in result.message


def test_throughput_check_bad_target_name(tmp_path):
    result = perf.throughput_check(BAD_NAME, str(tmp_path))
    assert result.status == "FAIL"


def test_latency_check_pass(tmp_path):
    dd_stderr = "4096 bytes copied, 0.001 s, 4.0 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)],
    ), patch("os.remove"):
        result = perf.latency_check(OST0, str(tmp_path))
    assert result.status == "PASS"
    assert result.value == 1.0


def test_latency_check_fail_slow(tmp_path):
    dd_stderr = "4096 bytes copied, 0.1 s, 0.04 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)],
    ), patch("os.remove"):
        result = perf.latency_check(OST0, str(tmp_path))
    assert result.status == "FAIL"


def test_run_perf_checks_runs_both_per_ost(tmp_path):
    from storage_validator.models import Topology

    topo = Topology(fsname="scratch", osts=[OST0])
    dd_stderr = "bytes copied, 1.0 s, 300 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[_ok(), shell_result(0, "", dd_stderr)] * 2,
    ), patch("os.remove"):
        results = perf.run_perf_checks(topo, str(tmp_path))
    kinds = {r.kind for r in results}
    assert kinds == {"throughput", "latency"}
    assert len(results) == 2
