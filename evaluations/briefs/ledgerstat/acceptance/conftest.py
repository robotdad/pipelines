"""Hidden acceptance suite for the `ledgerstat` brief.

This suite is NEVER visible to the pipeline under evaluation. It is copied into
the produced artifact only after the run has terminated, and is the ground truth
for criterion C1 (fidelity).

It deliberately re-derives the LedgerLine check character from the specification
prose rather than importing the reference implementation, so that a bug in the
reference parser cannot silently become the grading standard.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def check(prefix: str) -> str:
    """Base36 check character over the bytes of the first four joined fields."""
    return ALPHABET[sum(prefix.encode("utf-8")) % 36]


def record(oday: str, category: str, amount: str, memo: str) -> str:
    prefix = f"{oday}|{category}|{amount}|{memo}"
    return f"{prefix}|{check(prefix)}"


@pytest.fixture(scope="session")
def project_root() -> Path:
    root = os.environ.get("LEDGERSTAT_ROOT")
    return Path(root).resolve() if root else Path.cwd().resolve()


@pytest.fixture
def run(project_root):
    def _run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ledgerstat", *args],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return _run


@pytest.fixture
def ledger(tmp_path):
    def _write(body: str, name: str = "input.ldg") -> str:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    return _write
