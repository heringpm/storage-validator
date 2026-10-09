import csv
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


def _fake_single_elbencho_run_cmd(rate=None, lat_us=None):
    """Fake `run_cmd` for a single-OST `throughput_check`/`latency_check`
    call: `lfs setstripe` succeeds, `elbencho` writes one fake CSV row.
    """

    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            csv_path = args[args.index("--csvfile") + 1]
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["MiB/s [last]", "IO lat us [max]"]
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "MiB/s [last]": rate if rate is not None else 300.0,
                        "IO lat us [max]": lat_us if lat_us is not None else 1000.0,
                    }
                )
            return shell_result(0, "", "")
        raise AssertionError(f"unexpected args: {args}")

    return fake_run_cmd


def test_throughput_check_pass(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_single_elbencho_run_cmd(rate=300.0),
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "PASS"
    assert result.value == 300.0


def test_throughput_check_warn(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_single_elbencho_run_cmd(rate=100.0),
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "WARN"


def test_throughput_check_fail_low_rate(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_single_elbencho_run_cmd(rate=10.0),
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


def test_throughput_check_elbencho_failure(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            return shell_result(1, "", "elbencho: command not found")
        raise AssertionError(args)

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        result = perf.throughput_check(OST0, str(tmp_path))
    assert result.status == "FAIL"
    assert "elbencho" in result.message


def test_throughput_check_bad_target_name(tmp_path):
    result = perf.throughput_check(BAD_NAME, str(tmp_path))
    assert result.status == "FAIL"


def test_latency_check_pass(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_single_elbencho_run_cmd(lat_us=1000.0),
    ), patch("os.remove"):
        result = perf.latency_check(OST0, str(tmp_path))
    assert result.status == "PASS"
    assert result.value == 1.0


def test_latency_check_fail_slow(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_single_elbencho_run_cmd(lat_us=100000.0),
    ), patch("os.remove"):
        result = perf.latency_check(OST0, str(tmp_path))
    assert result.status == "FAIL"


def _fake_elbencho_run_cmd(rate_by_pool=None, lat_us_by_pool=None, csv_fieldname=None):
    """Build a fake `run_cmd` for pool tests: `lfs setstripe` always
    succeeds; `elbencho` writes a fake CSV row (picking a rate/latency by
    matching a pool-label keyword found in its `--csvfile` path, else a
    default) to the `--csvfile` path it's given, and reports success.
    """
    rate_by_pool = rate_by_pool or {}
    lat_by_pool = lat_us_by_pool or {}

    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            csv_path = args[args.index("--csvfile") + 1]
            joined = " ".join(args)
            rate = next((r for k, r in rate_by_pool.items() if k in joined), 300.0)
            lat_us = next((l for k, l in lat_by_pool.items() if k in joined), 1000.0)
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["MiB/s [last]", "IO lat us [max]"]
                )
                writer.writeheader()
                writer.writerow({"MiB/s [last]": rate, "IO lat us [max]": lat_us})
            return shell_result(0, "", "")
        raise AssertionError(f"unexpected args: {args}")

    return fake_run_cmd


def test_run_perf_checks_runs_both_per_ost(tmp_path):
    from storage_validator.models import Topology

    topo = Topology(fsname="scratch", osts=[OST0])
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(),
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

    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            csv_path = args[args.index("--csvfile") + 1]
            joined = " ".join(args)
            # fast for flash OST/pool, slow for archive OST/pool
            rate = 900.0 if ("flash" in joined or "OST0000" in joined) else 60.0
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["MiB/s [last]", "IO lat us [max]"]
                )
                writer.writeheader()
                writer.writerow({"MiB/s [last]": rate, "IO lat us [max]": 1000.0})
            return shell_result(0, "", "")
        raise AssertionError(f"unexpected args: {args}")

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

    pool_throughput = {
        r.pool: r for r in results if r.scope == "pool" and r.kind == "throughput"
    }
    assert set(pool_throughput) == {"flash", "archive"}
    assert pool_throughput["flash"].status == "PASS"
    assert pool_throughput["archive"].status == "WARN"


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


def test_pool_throughput_check_single_stripes_each_ost(tmp_path):
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        captured_cmds.append(args)
        return _fake_elbencho_run_cmd()(args, timeout=timeout)

    ost0 = Target(name="scratch-OST0000", kind="ost")
    ost1 = Target(name="scratch-OST0001", kind="ost")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        result = perf.pool_throughput_check("(unpooled)", [ost0, ost1], "scratch", str(tmp_path))

    setstripe_cmds = [c for c in captured_cmds if c[0] == "lfs"]
    assert len(setstripe_cmds) == 2
    assert all("-i" in c and "-c" in c and "1" in c for c in setstripe_cmds)
    elbencho_cmd = next(c for c in captured_cmds if c[0] == "elbencho")
    assert "-t" in elbencho_cmd
    assert elbencho_cmd[elbencho_cmd.index("-t") + 1] == "2"
    assert result.status == "PASS"
    assert result.value == 300.0


def test_pool_throughput_check_elbencho_failure(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            return shell_result(1, "", "elbencho: command not found")
        raise AssertionError(args)

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        result = perf.pool_throughput_check("flash", [OST0], "scratch", str(tmp_path))
    assert result.status == "FAIL"
    assert "elbencho" in result.message


def test_pool_latency_check_uses_elbencho_max_latency(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if args[0] == "elbencho":
            csv_path = args[args.index("--csvfile") + 1]
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["MiB/s [last]", "IO lat us [max]"]
                )
                writer.writeheader()
                writer.writerow({"MiB/s [last]": 100.0, "IO lat us [max]": 100000.0})
            return shell_result(0, "", "")
        raise AssertionError(args)

    ost0 = Target(name="scratch-OST0000", kind="ost", pool="flash")
    ost1 = Target(name="scratch-OST0001", kind="ost", pool="flash")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        result = perf.pool_latency_check("flash", [ost0, ost1], "scratch", str(tmp_path))

    assert result.value == 100.0  # 100000 us -> 100 ms
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
