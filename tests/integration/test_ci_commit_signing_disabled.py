"""A scratch repo must be able to commit even when ambient config signs.

WHY THIS EXISTS. Dozens of tests build throwaway git repos under ``tmp_path``
and commit into them. They inherit the AMBIENT global git config, and the
operator's dotfiles (``~/.dotfiles/src/.gitconfig``) set ``commit.gpgsign =
true`` with ``gpg.format = ssh`` and a ``user.signingkey`` pointing at
``~/.ssh/id_ed25519_scitex.pub``. On 2026-08-09 that key was absent, so every
such commit failed:

    error: Couldn't load public key .../id_ed25519_scitex.pub: No such file
    fatal: failed to write commit object
    exit 128

13 failures and 120 errors, on EVERY pull request regardless of its diff.

THE SECOND LINE IS WHY THIS TEST EXISTS RATHER THAN A COMMENT. "failed to
write commit object" reads as disk I/O. It produced two confidently-wrong root
causes broadcast to other agents — a shared-runner git-identity fault, then
ENOSPC (the disk WAS also full, separately, which made the wrong answer fit).
The real cause was one line higher in the log the whole time.

These tests reproduce the exact condition and pin the fix, so the next person
meets a red test naming signing instead of a symptom pointing at hardware.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "ci" / "run-in-sif.sh"


def _git(repo: Path, *args: str, env: dict | None = None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def signing_repo(tmp_path):
    """A repo whose config signs with a key that does not exist.

    The GIT_CONFIG_* vars are cleared for the duration and restored after.
    That is not hygiene, it is CORRECTNESS: in CI ``run-in-sif.sh`` exports the
    very override under test, so without clearing it the "reproduce the
    failure" cases would see signing already disabled and assert the opposite
    of what they mean.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("GIT_CONFIG_")}
    for key in saved:
        del os.environ[key]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    for key, value in (
        ("user.email", "t@example.com"),
        ("user.name", "t"),
        ("commit.gpgsign", "true"),
        ("gpg.format", "ssh"),
        ("user.signingkey", str(tmp_path / "absent-key.pub")),
    ):
        _git(repo, "config", key, value)
    try:
        yield repo
    finally:
        for key, value in saved.items():
            os.environ[key] = value


def test_missing_signing_key_breaks_commit_without_the_fix(signing_repo):
    # Arrange
    repo = signing_repo
    # Act
    result = _git(repo, "commit", "--allow-empty", "-q", "-m", "seed")
    # Assert
    assert result.returncode != 0, (
        "expected the unfixed condition to fail — if this passes, the ambient "
        "signing config changed and this whole guard needs revisiting"
    )


def test_the_failure_names_signing_not_disk(signing_repo):
    # Arrange
    repo = signing_repo
    # Act
    result = _git(repo, "commit", "--allow-empty", "-q", "-m", "seed")
    # Assert — the misleading half is 'failed to write commit object'; the
    # diagnostic half is the public-key line. Pin that it is present, because
    # reading only the former is exactly how this cost an afternoon.
    assert "public key" in result.stderr, result.stderr


def test_signing_override_lets_a_scratch_repo_commit(signing_repo):
    # Arrange
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "tag.gpgsign", "GIT_CONFIG_VALUE_1": "false",
    })
    # Act
    result = _git(signing_repo, "commit", "--allow-empty", "-q", "-m", "seed", env=env)
    # Assert
    assert result.returncode == 0, result.stderr


def test_ci_entrypoint_disables_commit_signing():
    # Arrange
    text = _SCRIPT.read_text(encoding="utf-8")
    # Act
    present = "GIT_CONFIG_KEY_0=commit.gpgsign" in text
    # Assert
    assert present, (
        "run-in-sif.sh must disable commit signing for the test suite; without "
        "it every scratch-repo commit dies when the signing key is absent."
    )


def test_ci_entrypoint_keeps_the_override_additive():
    # Arrange
    text = _SCRIPT.read_text(encoding="utf-8")
    # Act
    replaces_global = "GIT_CONFIG_GLOBAL=" in text
    # Assert — GIT_CONFIG_COUNT layers ON TOP of ambient config; setting
    # GIT_CONFIG_GLOBAL instead would REPLACE it wholesale and silently drop
    # safe.directory and anything else the runner depends on.
    assert not replaces_global, (
        "use the additive GIT_CONFIG_COUNT overrides, not GIT_CONFIG_GLOBAL, "
        "so the rest of the ambient git config survives"
    )
