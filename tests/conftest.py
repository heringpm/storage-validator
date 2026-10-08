"""Shared pytest fixtures: loading fixture text and building fake CommandResults."""

from __future__ import annotations

from pathlib import Path

import pytest

from storage_validator.backends.lustre.shell import CommandResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def fake_result(stdout: str, returncode: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fixtures():
    """Expose the loader function to tests as `fixtures('name.txt')`."""
    return load_fixture


@pytest.fixture
def make_result():
    return fake_result
