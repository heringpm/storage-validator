from storage_validator.backends.lustre import shell


def test_run_cmd_dry_run_does_not_execute_and_returns_success(monkeypatch):
    printed = []
    monkeypatch.setattr(shell, "DRY_RUN_SINK", printed.append)
    shell.set_dry_run(True)
    try:
        result = shell.run_cmd(["lfs", "setstripe", "-i", "0", "-c", "1", "/mnt/x"])
    finally:
        shell.set_dry_run(False)

    assert result.ok
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert len(printed) == 1
    assert "lfs setstripe -i 0 -c 1 /mnt/x" in printed[0]


def test_run_cmd_executes_normally_when_dry_run_disabled():
    assert shell.DRY_RUN is False
    result = shell.run_cmd(["true"])
    assert result.ok


def test_set_dry_run_toggles_module_flag():
    assert shell.DRY_RUN is False
    shell.set_dry_run(True)
    try:
        assert shell.DRY_RUN is True
    finally:
        shell.set_dry_run(False)
    assert shell.DRY_RUN is False
