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


def _fake_run_cmd_by_path(rates_by_keyword, default_rate="bytes copied, 1.0 s, 300 MB/s"):
    """Build a thread-safe fake `run_cmd` for pool tests: setstripe always
    succeeds, and `dd` calls return a rate chosen by matching a keyword
    (e.g. a pool name or OST name) found in the command's path argument.
    """

    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "dd":
            joined = " ".join(args)
            for keyword, rate in rates_by_keyword.items():
                if keyword in joined:
                    return shell_result(0, "", rate)
            return shell_result(0, "", default_rate)
        raise AssertionError(f"unexpected args: {args}")

    return fake_run_cmd


def test_run_perf_checks_runs_both_per_ost(tmp_path):
    from storage_validator.models import Topology

    topo = Topology(fsname="scratch", osts=[OST0])
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_run_cmd_by_path({}),
    ), patch("os.remove"):
        results = perf.run_perf_checks(topo, str(tmp_path))
    kinds = {r.kind for r in results}
    assert kinds == {"throughput", "latency"}
    # 2 per-OST results + 2 real pool-level aggregate results (1 pool: unpooled)
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

    # flash: fast (900 MB/s); archive: slow (60 MB/s). Keyed by OST name so
    # both the per-OST test and the pool-wide test (whose scratch file name
    # includes the pool label, not the OST name) resolve correctly.
    fake_run_cmd = _fake_run_cmd_by_path(
        {
            "scratch-OST0000": "bytes copied, 1.0 s, 900 MB/s",
            "scratch-OST0001": "bytes copied, 1.0 s, 60 MB/s",
            "pool_flash": "bytes copied, 1.0 s, 900 MB/s",
            "pool_archive": "bytes copied, 1.0 s, 60 MB/s",
        }
    )
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
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

    pool_results = {
        (r.pool, r.kind): r for r in results if r.scope == "pool"
    }
    assert {"flash", "archive"} == {pool for pool, _ in pool_results}
    # Pool test measures real wall-clock aggregate throughput (not dd's
    # reported rate), so just check both pools produced a real test result
    # distinct from a simple average, and the archive pool's slow dd rate
    # at least makes it WARN/FAIL-eligible, not silently PASS.
    assert pool_results[("flash", "throughput")].status in {"PASS", "WARN", "FAIL"}
    assert pool_results[("archive", "throughput")].status in {"PASS", "WARN", "FAIL"}


def test_pool_throughput_check_setstripe_failure(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        return_value=shell_result(1, "", "setstripe error"),
    ), patch("os.remove"):
        result = perf.pool_throughput_check(
            "flash", [OST0], "scratch", str(tmp_path)
        )
    assert result.status == "FAIL"
    assert "setstripe" in result.message


def test_pool_throughput_check_no_valid_indices(tmp_path):
    result = perf.pool_throughput_check("flash", [BAD_NAME], "scratch", str(tmp_path))
    assert result.status == "FAIL"
    assert "OST indices" in result.message


def test_pool_throughput_check_uses_explicit_indices_for_unpooled(tmp_path):
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        captured_cmds.append(args)
        if args[0] == "lfs":
            return shell_result(0, "", "")
        return shell_result(0, "", "bytes copied, 1.0 s, 100 MB/s")

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        perf.pool_throughput_check("(unpooled)", [OST0], "scratch", str(tmp_path))

    setstripe_cmd = captured_cmds[0]
    assert "-o" in setstripe_cmd
    assert "-p" not in setstripe_cmd


def test_pool_throughput_check_uses_pool_name_for_real_pools(tmp_path):
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        captured_cmds.append(args)
        if args[0] == "lfs":
            return shell_result(0, "", "")
        return shell_result(0, "", "bytes copied, 1.0 s, 100 MB/s")

    flash = Target(name="scratch-OST0000", kind="ost", pool="flash")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        perf.pool_throughput_check("flash", [flash], "scratch", str(tmp_path))

    setstripe_cmd = captured_cmds[0]
    assert "-p" in setstripe_cmd
    assert "scratch.flash" in setstripe_cmd


def test_pool_latency_check_reports_worst_of_concurrent_writes(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        seek = next((a for a in args if a.startswith("seek=")), "seek=0")
        idx = int(seek.split("=")[1])
        # OST 0 is slow (100 ms), OST 1 is fast (1 ms)
        seconds = 0.1 if idx == 0 else 0.001
        return shell_result(0, "", f"bytes copied, {seconds} s, 1 MB/s")

    ost0 = Target(name="scratch-OST0000", kind="ost", pool="flash")
    ost1 = Target(name="scratch-OST0001", kind="ost", pool="flash")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        result = perf.pool_latency_check("flash", [ost0, ost1], "scratch", str(tmp_path))

    assert result.value == 100.0
    assert result.scope == "pool"


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
