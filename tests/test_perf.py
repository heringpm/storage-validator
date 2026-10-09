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
    # 2 per-OST results + 2 pool-level aggregate results (1 pool: unpooled)
    assert len(results) == 4
    pool_results = [r for r in results if r.scope == "pool"]
    assert len(pool_results) == 2
    assert {r.target for r in pool_results} == {"pool:(unpooled)"}


def test_run_perf_checks_aggregates_per_pool_with_custom_thresholds(tmp_path):
    from storage_validator.config import PerfThresholds
    from storage_validator.models import Topology

    flash = Target(name="scratch-OST0000", kind="ost", uuid="u0", pool="flash")
    archive = Target(name="scratch-OST0001", kind="ost", uuid="u1", pool="archive")
    topo = Topology(fsname="scratch", osts=[flash, archive])

    # flash: fast (900 MB/s, fast latency); archive: slow (60 MB/s)
    flash_dd = "bytes copied, 1.0 s, 900 MB/s"
    archive_dd = "bytes copied, 1.0 s, 60 MB/s"
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=[
            _ok(), shell_result(0, "", flash_dd),   # flash throughput
            _ok(), shell_result(0, "", flash_dd),   # flash latency
            _ok(), shell_result(0, "", archive_dd),  # archive throughput
            _ok(), shell_result(0, "", archive_dd),  # archive latency
        ],
    ), patch("os.remove"):
        results = perf.run_perf_checks(
            topo,
            str(tmp_path),
            default_thresholds=PerfThresholds(
                warn_mbps=200, fail_mbps=50, warn_ms=10, fail_ms=50
            ),
            pool_thresholds={
                "archive": PerfThresholds(
                    warn_mbps=70, fail_mbps=30, warn_ms=50, fail_ms=200
                )
            },
        )

    archive_throughput = next(
        r for r in results if r.target == "scratch-OST0001" and r.kind == "throughput"
    )
    # 60 MB/s is below the default warn (200) but within archive's custom
    # warn threshold (70 is warn, 30 is fail) -> WARN not FAIL.
    assert archive_throughput.status == "WARN"

    pool_throughput = {
        r.pool: r for r in results if r.scope == "pool" and r.kind == "throughput"
    }
    assert set(pool_throughput) == {"flash", "archive"}
    assert pool_throughput["flash"].status == "PASS"
    assert pool_throughput["archive"].status == "WARN"


def test_resolve_thresholds_falls_back_to_default():
    from storage_validator.config import PerfThresholds

    default = PerfThresholds(warn_mbps=1, fail_mbps=1, warn_ms=1, fail_ms=1)
    custom = {"flash": PerfThresholds(warn_mbps=2, fail_mbps=2, warn_ms=2, fail_ms=2)}
    no_pool_target = Target(name="scratch-OST0000", kind="ost")
    archive_target = Target(name="scratch-OST0001", kind="ost", pool="archive")
    flash_target = Target(name="scratch-OST0002", kind="ost", pool="flash")

    assert perf.resolve_thresholds(no_pool_target, default, custom) is default
    assert perf.resolve_thresholds(archive_target, default, custom) is default
    assert perf.resolve_thresholds(flash_target, default, custom) is custom["flash"]
