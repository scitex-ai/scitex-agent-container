"""Shared fixtures for the git_identity_hooks tests.

Drives the real ``enforce_commit_author_allowlist.sh`` shell hook against
real ephemeral git repos (no mocks, no patches) — the fleet rule for hook
tests. The script is a ``.sh`` asset with no ``.py`` source counterpart,
so — like the ``_real.py`` integration pattern (see
``02_package/06_project-structure-tests.md``) — these tests live under
``tests/integration/`` to stay OUT of PS-204's ``tests/<pkg>/`` mirror
scope while ``pytest tests/`` still collects and runs them.

The hook is resolved by repo-relative path so a refactor of the asset
location is caught at collection time, not at spawn time.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

HOOK_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "git_identity_hooks"
)
HOOK_SCRIPT = HOOK_DIR / "enforce_commit_author_allowlist.sh"

ALLOWLISTED_EMAIL = "ywatanabe@scitex.ai"
ALLOWLISTED_NAME = "Yusuke Watanabe"

# The AGENT identity (operator-approved 2026-08-12): a SECOND verified email
# on the same GitHub account (ywatanabe1989), so it clears the CLA gate while
# making agent-authored commits distinguishable from the operator's own in
# `git log`. Allowlisted alongside — NOT instead of — the human identity.
AGENT_EMAIL = "agent@scitex.ai"
AGENT_NAME = "scitex-agent-container"

# Still NOT allowlisted: the `agent@<host>` shape from the scitex-hpc
# 2026-07-05 incident maps to no GitHub account. Deliberately one character
# away from AGENT_EMAIL's domain, so a regex that over-matches is caught.
NON_ALLOWLISTED_EMAIL = "agent@scitex-hpc"

# Identity/allowlist env vars that would make the hook's resolution
# non-deterministic; scrubbed from every hook invocation so each test
# controls its own inputs via repo config / command / explicit env.
_SCRUB = (
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_NAME",
    "CC_CLA_ALLOWED_EMAILS",
    "CC_ALLOW_CLA_AUTHOR",
)


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if extra:
        env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_clean_env(),
    )
    return res.stdout.strip()


def _make_repo(path: Path, email: str, name: str = "someone") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "--initial-branch=feature/x")
    _git(path, "config", "user.email", email)
    _git(path, "config", "user.name", name)
    _git(path, "commit", "--allow-empty", "-q", "-m", "seed")
    return path


def run_hook(
    command: str, cwd: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Invoke the hook with a Bash-tool PreToolUse payload; return the
    completed process so tests assert on returncode/stderr directly."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_clean_env(extra_env),
    )


@pytest.fixture
def good_repo(tmp_path: Path) -> Iterator[Path]:
    yield _make_repo(tmp_path / "good", ALLOWLISTED_EMAIL, ALLOWLISTED_NAME)


@pytest.fixture
def agent_repo(tmp_path: Path) -> Iterator[Path]:
    yield _make_repo(tmp_path / "agentid", AGENT_EMAIL, AGENT_NAME)


@pytest.fixture
def bad_repo(tmp_path: Path) -> Iterator[Path]:
    yield _make_repo(tmp_path / "bad", NON_ALLOWLISTED_EMAIL, "agent")
