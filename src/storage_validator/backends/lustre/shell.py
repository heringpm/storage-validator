"""Thin subprocess wrapper used by all Lustre backend modules.

Centralizing subprocess calls here makes it trivial to mock in tests via
`unittest.mock.patch("storage_validator.backends.lustre.shell.run_cmd")`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


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
    """
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
