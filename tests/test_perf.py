import csv
from unittest.mock import patch

from storage_validator.backends.lustre import perf
from storage_validator.models import Target

OST0 = Target(name="scratch-OST0000", kind="ost", uuid="ost0_uuid")
BAD_NAME = Target(name="not-an-ost", kind="ost", uuid="x")


def shell_result(returncode, stdout="", stderr=""):
    from storage_validator.backends.lustre.shell import CommandResult

    return CommandResult(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_ost_index():
    assert perf.ost_index(OST0) == 0
    assert perf.ost_index(Target(name="scratch-OST000a", kind="ost")) == 10
    assert perf.ost_index(BAD_NAME) is None


def test_detect_cpu_thread_count_parses_lscpu():
    def fake_run_cmd(args, timeout=30):
        return shell_result(0, "Architecture: x86_64\nCPU(s):  16\nVendor ID: GenuineIntel\n", "")

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ):
        assert perf.detect_cpu_thread_count() == 16


def test_detect_cpu_thread_count_falls_back_on_missing_lscpu():
    from storage_validator.backends.lustre import shell as shell_mod

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=shell_mod.CommandError(["lscpu"], "command not found"),
    ), patch("os.cpu_count", return_value=4):
        assert perf.detect_cpu_thread_count() == 4


def _fake_elbencho_run_cmd(rate=300.0, lat_us=1000.0):
    """Fake `run_cmd`: `lfs setstripe` succeeds; `elbencho` writes one fake
    CSV row (same rate/latency for every call) to the `--csvfile` path.
    """

    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        if "elbencho" in args[0]:
            csv_path = args[args.index("--csvfile") + 1]
            with open(csv_path, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["MiB/s [last]", "IO lat us [max]"])
                writer.writeheader()
                writer.writerow({"MiB/s [last]": rate, "IO lat us [max]": lat_us})
            return shell_result(0, "", "")
        raise AssertionError(f"unexpected args: {args}")

    return fake_run_cmd


def test_ost_rw_check_pass(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(rate=300.0, lat_us=1000.0),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.ost_rw_check(OST0, str(tmp_path), threads=1)
    assert wt.status == "PASS"
    assert wt.value == 300.0
    assert wl.value == 1.0
    assert wt.io_mode == "write"
    assert rt.io_mode == "read"
    assert rt.value == 300.0


def test_ost_rw_check_warn(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(rate=100.0),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.ost_rw_check(OST0, str(tmp_path), threads=1)
    assert wt.status == "WARN"
    assert rt.status == "WARN"
    assert rt.io_mode == "read"


def test_ost_rw_check_fail_low_rate(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(rate=10.0),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.ost_rw_check(OST0, str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert rt.status == "FAIL"


def test_ost_rw_check_setstripe_failure(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        return_value=shell_result(1, "", "setstripe error"),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.ost_rw_check(OST0, str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert wl.status == "FAIL"
    assert rt.status == "FAIL"
    assert rl.status == "FAIL"
    assert "setstripe" in wt.message


def test_ost_rw_check_elbencho_failure(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        return shell_result(1, "", "elbencho: command not found")

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.ost_rw_check(OST0, str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert "elbencho" in wt.message


def test_ost_rw_check_uses_configured_threads_and_one_file_per_thread(tmp_path):
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        captured_cmds.append(args)
        return _fake_elbencho_run_cmd()(args, timeout=timeout)

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        perf.ost_rw_check(OST0, str(tmp_path), threads=8)

    setstripe_cmds = [c for c in captured_cmds if c[0] == "lfs"]
    assert len(setstripe_cmds) == 8  # one scratch file per worker thread
    elbencho_cmds = [c for c in captured_cmds if "elbencho" in c[0]]
    assert len(elbencho_cmds) == 2  # one write pass, one read pass
    for cmd in elbencho_cmds:
        assert cmd[cmd.index("-t") + 1] == "8"
        assert "--direct" in cmd
    assert "-w" in elbencho_cmds[0]
    assert "--sync" in elbencho_cmds[0]  # fsync before exiting the write pass
    assert "-r" in elbencho_cmds[1]
    assert "-w" not in elbencho_cmds[1]
    assert "--sync" not in elbencho_cmds[1]
    # Both passes target the same set of per-thread scratch file paths.
    write_paths = [p for p in elbencho_cmds[0][elbencho_cmds[0].index("--csvfile") + 2:] if p != "--sync"]
    read_paths = elbencho_cmds[1][elbencho_cmds[1].index("--csvfile") + 2:]
    assert write_paths == read_paths
    assert len(set(write_paths)) == 8


def test_ost_rw_check_defaults_threads_to_detected_cpu_count(tmp_path):
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        if args == ["lscpu"]:
            return shell_result(0, "CPU(s): 6\n", "")
        captured_cmds.append(args)
        return _fake_elbencho_run_cmd()(args, timeout=timeout)

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        perf.ost_rw_check(OST0, str(tmp_path))

    elbencho_cmd = next(c for c in captured_cmds if "elbencho" in c[0])
    assert elbencho_cmd[elbencho_cmd.index("-t") + 1] == "6"


def test_ost_rw_check_bad_target_name(tmp_path):
    wt, wl, rt, rl = perf.ost_rw_check(BAD_NAME, str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert wl.status == "FAIL"
    assert rt.status == "FAIL"
    assert rl.status == "FAIL"


def test_run_perf_checks_runs_read_and_write_per_ost(tmp_path):
    from storage_validator.models import Topology

    topo = Topology(fsname="scratch", osts=[OST0])
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(),
    ), patch("os.remove"):
        results = perf.run_perf_checks(topo, str(tmp_path), threads=1)
    kinds = {r.kind for r in results}
    assert kinds == {"throughput", "latency"}
    # 1 OST check (write+read, 2 kinds) + 1 pool check (write+read, 2 kinds) = 8
    assert len(results) == 8
    io_modes = {r.io_mode for r in results}
    assert io_modes == {"write", "read"}
    pool_results = [r for r in results if r.scope == "pool"]
    assert len(pool_results) == 4
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
        csv_path = args[args.index("--csvfile") + 1]
        joined = " ".join(args)
        # fast for flash OST/pool, slow for archive OST/pool
        rate = 900.0 if ("flash" in joined or "OST0000" in joined) else 60.0
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["MiB/s [last]", "IO lat us [max]"])
            writer.writeheader()
            writer.writerow({"MiB/s [last]": rate, "IO lat us [max]": 1000.0})
        return shell_result(0, "", "")

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
            threads=1,
        )

    archive_throughput = next(
        r for r in results
        if r.target == "scratch-OST0001" and r.kind == "throughput" and r.io_mode == "write"
    )
    # 60 MB/s is below the default warn (200) but within archive's custom
    # warn threshold (70 is warn, 30 is fail) -> WARN not FAIL.
    assert archive_throughput.status == "WARN"

    pool_throughput = {
        r.pool: r for r in results
        if r.scope == "pool" and r.kind == "throughput" and r.io_mode == "write"
    }
    assert set(pool_throughput) == {"flash", "archive"}
    assert pool_throughput["flash"].status == "PASS"
    assert pool_throughput["archive"].status == "WARN"


def test_pool_rw_check_setstripe_failure(tmp_path):
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        return_value=shell_result(1, "", "setstripe error"),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.pool_rw_check("flash", [OST0], str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert "setstripe" in wt.message


def test_pool_rw_check_no_valid_indices(tmp_path):
    wt, wl, rt, rl = perf.pool_rw_check("flash", [BAD_NAME], str(tmp_path))
    assert wt.status == "FAIL"
    assert "OST indices" in wt.message


def test_pool_rw_check_single_stripes_each_file_across_osts(tmp_path):
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
        wt, wl, rt, rl = perf.pool_rw_check(
            "(unpooled)", [ost0, ost1], str(tmp_path), threads=4
        )

    setstripe_cmds = [c for c in captured_cmds if c[0] == "lfs"]
    # one scratch file per worker thread (never multiplied by OST count)
    assert len(setstripe_cmds) == 4
    assert all("-i" in c and "-c" in c and "1" in c for c in setstripe_cmds)
    elbencho_cmds = [c for c in captured_cmds if "elbencho" in c[0]]
    assert len(elbencho_cmds) == 2  # one write pass, one read pass
    for cmd in elbencho_cmds:
        assert cmd[cmd.index("-t") + 1] == "4"
    assert wt.status == "PASS"
    assert wt.value == 300.0
    assert rt.io_mode == "read"
    assert rt.value == 300.0


def test_pool_rw_check_elbencho_failure(tmp_path):
    def fake_run_cmd(args, timeout=30):
        if args[0] == "lfs":
            return shell_result(0, "", "")
        return shell_result(1, "", "elbencho: command not found")

    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.pool_rw_check("flash", [OST0], str(tmp_path), threads=1)
    assert wt.status == "FAIL"
    assert "elbencho" in wt.message


def test_pool_rw_check_uses_elbencho_max_latency(tmp_path):
    ost0 = Target(name="scratch-OST0000", kind="ost", pool="flash")
    ost1 = Target(name="scratch-OST0001", kind="ost", pool="flash")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=_fake_elbencho_run_cmd(rate=100.0, lat_us=100000.0),
    ), patch("os.remove"):
        wt, wl, rt, rl = perf.pool_rw_check(
            "flash", [ost0, ost1], str(tmp_path), threads=1
        )

    assert wl.value == 100.0  # 100000 us -> 100 ms
    assert wl.scope == "pool"


def test_pool_rw_check_read_reuses_write_pass_scratch_files(tmp_path):
    """A pool check must run a write pass to populate every worker thread's
    scratch file, then a read pass reusing those same scratch paths."""
    captured_cmds = []

    def fake_run_cmd(args, timeout=30):
        captured_cmds.append(args)
        return _fake_elbencho_run_cmd()(args, timeout=timeout)

    ost0 = Target(name="scratch-OST0000", kind="ost", pool="flash")
    ost1 = Target(name="scratch-OST0001", kind="ost", pool="flash")
    with patch(
        "storage_validator.backends.lustre.perf.shell.run_cmd",
        side_effect=fake_run_cmd,
    ), patch("os.remove"):
        perf.pool_rw_check("flash", [ost0, ost1], str(tmp_path), threads=2)

    elbencho_cmds = [c for c in captured_cmds if "elbencho" in c[0]]
    assert len(elbencho_cmds) == 2
    assert "-w" in elbencho_cmds[0]
    assert "--sync" in elbencho_cmds[0]  # fsync before exiting the write pass
    assert "-r" in elbencho_cmds[1]
    assert "-w" not in elbencho_cmds[1]
    assert "--sync" not in elbencho_cmds[1]
    # Both passes target the same scratch file paths.
    write_paths = [p for p in elbencho_cmds[0][elbencho_cmds[0].index("--csvfile") + 2:] if p != "--sync"]
    read_paths = elbencho_cmds[1][elbencho_cmds[1].index("--csvfile") + 2:]
    assert write_paths == read_paths
    assert len(set(write_paths)) == 2


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
