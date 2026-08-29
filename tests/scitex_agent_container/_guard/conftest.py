"""Fixtures for the unrequested-deletion guard tests.

Real git repositories on disk, never a mock: the guard's whole job is to
read a baseline out of git, and a faked ref would test the fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# The incident shape, verbatim in spirit: ``pipeline.py`` imports two
# classes from ``transforms.py``. The 2026 local-model failure deleted
# both while "adding" a function, and never said so in its summary.
TRANSFORMS_BEFORE = '''\
"""Transform helpers imported by pipeline.py."""


class Scaler:
    def __init__(self, factor):
        self.factor = factor

    def apply(self, values):
        return [v * self.factor for v in values]


class Normalizer:
    def __init__(self, lo, hi):
        self.lo, self.hi = lo, hi

    def apply(self, values):
        span = self.hi - self.lo or 1
        return [(v - self.lo) / span for v in values]


def identity(values):
    return list(values)
'''

TRANSFORMS_INCIDENT = '''\
"""Transform helpers imported by pipeline.py."""


def identity(values):
    return list(values)


def clip(values, lo, hi):
    return [min(max(v, lo), hi) for v in values]
'''

TRANSFORMS_CLEAN_FEATURE = TRANSFORMS_BEFORE + '''

def clip(values, lo, hi):
    return [min(max(v, lo), hi) for v in values]
'''

PIPELINE = "from transforms import Normalizer, Scaler  # noqa: F401\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed two-file repo. HEAD is the baseline; tree is clean."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    _git(root, "config", "user.email", "guard-test@example.com")
    _git(root, "config", "user.name", "guard test")
    (root / "transforms.py").write_text(TRANSFORMS_BEFORE)
    (root / "pipeline.py").write_text(PIPELINE)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def not_a_repo(tmp_path: Path) -> Path:
    """A plain directory with python in it — and no git anywhere above."""
    root = tmp_path / "loose"
    root.mkdir()
    (root / "transforms.py").write_text(TRANSFORMS_BEFORE)
    return root
