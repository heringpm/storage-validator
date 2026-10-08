from unittest.mock import patch

from storage_validator.backends.lustre import discovery


def test_discover_fsname(make_result):
    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        return_value=make_result("scratch\n"),
    ):
        assert discovery.discover_fsname() == "scratch"


def test_discover_targets(fixtures, make_result):
    raw = fixtures("lctl_dl.txt")
    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        return_value=make_result(raw),
    ):
        targets = discovery.discover_targets()

    # Garbage line and non mdt/obdfilter lines (mgc/lov/osc) must be skipped.
    names = {t.name for t in targets}
    assert names == {
        "scratch-MDT0000",
        "scratch-OST0000",
        "scratch-OST0001",
        "scratch-OST0002",
    }
    mdts = [t for t in targets if t.kind == "mdt"]
    osts = [t for t in targets if t.kind == "ost"]
    assert len(mdts) == 1
    assert len(osts) == 3
    inactive = next(t for t in targets if t.name == "scratch-OST0001")
    assert inactive.state == "IN"


def test_discover_ost_activation(fixtures, make_result):
    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        return_value=make_result(fixtures("lfs_osts.txt")),
    ):
        states = discovery.discover_ost_activation()
    assert states["scratch-OST0000_UUID"] == "ACTIVE"
    assert states["scratch-OST0001_UUID"] == "INACTIVE"


def test_discover_df(fixtures, make_result):
    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        return_value=make_result(fixtures("lfs_df.txt")),
    ):
        df = discovery.discover_df()
    assert df["scratch-OST0001_UUID"]["use_pct"] == 95
    assert df["scratch-MDT0000_UUID"]["use_pct"] == 10
    assert "filesystem_summary:" not in df


def test_discover_pools(fixtures, make_result):
    pool_list_raw = fixtures("lfs_pool_list.txt")

    def fake_run_cmd(args, timeout=30):
        if args == ["lfs", "pool_list", "scratch"]:
            return make_result(pool_list_raw)
        if args == ["lfs", "pool_list", "scratch.flash"]:
            return make_result("Pool: scratch.flash\nscratch-OST0000_UUID\n")
        if args == ["lfs", "pool_list", "scratch.archive"]:
            return make_result("Pool: scratch.archive\nscratch-OST0002_UUID\n")
        raise AssertionError(f"unexpected args: {args}")

    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        side_effect=fake_run_cmd,
    ):
        pools = discovery.discover_pools("scratch")

    assert {p.name for p in pools} == {"flash", "archive"}
    flash = next(p for p in pools if p.name == "flash")
    assert flash.osts == ["scratch-OST0000_UUID"]


def test_discover_targets_skips_malformed_lines(make_result):
    raw = "this is not a valid lctl dl line\n  garbage 1 2 3\n"
    with patch(
        "storage_validator.backends.lustre.discovery.shell.run_cmd",
        return_value=make_result(raw),
    ):
        targets = discovery.discover_targets()
    assert targets == []
