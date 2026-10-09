"""Thin subprocess wrapper used by all Lustre backend modules.

Centralizing subprocess calls here makes it trivial to mock in tests via
`unittest.mock.patch("storage_validator.backends.lustre.shell.run_cmd")`.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

# When True, `run_cmd` only prints the command it would have run (via
# `DRY_RUN_SINK`, a callable taking the formatted command line) instead of
# executing it, returning a canned successful result. Toggled on by the
# CLI's `--dry-run` flag.
DRY_RUN = False
DRY_RUN_SINK = print


def set_dry_run(enabled: bool) -> None:
    global DRY_RUN
    DRY_RUN = enabled


class CommandError(Exception):
    """Raised when a command fails to execute or times out."""

    def __init__(self, args: list[str], message: str):
        self.args_ = args
        self.message = message
        super().__init__(f"Command {args!r} failed: {message}")


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_cmd(args: list[str], timeout: float = 30) -> CommandResult:
    """Run a command and capture stdout/stderr as text.

    Raises CommandError if the binary is missing or the call times out.
    A non-zero return code does NOT raise; callers inspect `.ok`/`.returncode`.

    If `DRY_RUN` is enabled, the command is only printed (via `DRY_RUN_SINK`)
    and not actually executed; a canned successful `CommandResult` (empty
    stdout/stderr, returncode 0) is returned instead.
    """
    if DRY_RUN:
        DRY_RUN_SINK(f"[DRY RUN] {shlex.join(args)}")
        return CommandResult(args=args, returncode=0, stdout="", stderr="")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(args, f"command not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(args, f"timed out after {timeout}s") from exc

    return CommandResult(
        args=args,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
