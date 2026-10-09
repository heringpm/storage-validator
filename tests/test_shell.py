from storage_validator.backends.lustre import shell


def test_run_cmd_always_executes_regardless_of_dry_run_flag():
    """`run_cmd` itself never special-cases DRY_RUN -- discovery/health
    commands (read-only `lctl`/`lfs` calls) must always run for real, even
    in dry-run mode, so dry-run can discover the actual topology. Only
    perf.py's `lfs setstripe`/`elbencho` call sites check the flag
    themselves, via `shell.print_dry_run`.
    """
    shell.set_dry_run(True)
    try:
        result = shell.run_cmd(["true"])
    finally:
        shell.set_dry_run(False)
    assert result.ok


def test_print_dry_run_formats_and_prints_command(monkeypatch):
    printed = []
    monkeypatch.setattr(shell, "DRY_RUN_SINK", printed.append)
    shell.print_dry_run(["lfs", "setstripe", "-i", "0", "-c", "1", "/mnt/x"])
    assert len(printed) == 1
    assert "[DRY RUN]" in printed[0]
    assert "lfs setstripe -i 0 -c 1 /mnt/x" in printed[0]


def test_set_dry_run_toggles_module_flag():
    assert shell.DRY_RUN is False
    shell.set_dry_run(True)
    try:
        assert shell.DRY_RUN is True
    finally:
        shell.set_dry_run(False)
    assert shell.DRY_RUN is False
