"""Real-git helpers for lifecycle tests that require a launchable spec."""

from __future__ import annotations

import subprocess
from pathlib import Path


def establish_test_spec_authority(spec_path: Path) -> Path:
    """Commit ``spec_path`` on a clean, current ``develop`` test authority."""
    root = spec_path.parent
    remote = root.parent / f".{root.name}-authority.git"
    _git(root.parent, "init", "--bare", str(remote))
    _git(root.parent, "init", "-b", "develop", str(root))
    _git(root, "config", "user.email", "tests@scitex.invalid")
    _git(root, "config", "user.name", "SciTeX Tests")
    _git(root, "add", spec_path.name)
    _git(root, "commit", "-m", "test spec authority")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-u", "origin", "develop")
    return spec_path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
