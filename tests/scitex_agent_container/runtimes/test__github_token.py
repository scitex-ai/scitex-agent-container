"""``GITHUB_TOKEN`` must cross into the agent container.

INCIDENT 2026-08-09, scitex-compute-04. Measured end to end::

    login shell      GITHUB_TOKEN len=40   <- present and correct
    non-login shell  GITHUB_TOKEN len=0    <- gone
    ~/.config/gh/                          -> EMPTY

The fleet secrets live in ``~/.bash.d/secrets``, which only a LOGIN shell
sources; sac starts containers without one and passed nothing through. So
``gh`` inside every container reported "not logged into any GitHub hosts",
three agents each finished tested work, and NOT ONE could open a pull
request — two finished fixes had to be opened by the operator's own
session on their authors' behalf.

The secrets were never missing. They simply never crossed the boundary.

No mocks (PA-306): a real ``dest/.env`` on disk, and the pool is driven
through the real ``SAC_SECRETS_ENVRC`` seam with a real secrets file, the
same way ``_cct_token_pool`` is exercised. Token values here are obvious
fakes and are asserted on deliberately — they are test fixtures, not
secrets.

AAA markers, one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._github_token import ensure_github_token

_FAKE_TOKEN = "ghp-not-a-real-token-0000000000"


class _Config:
    """Minimal stand-in for AgentConfig — only `name` is read."""

    def __init__(self, name: str = "an-agent") -> None:
        self.name = name


@pytest.fixture
def secrets_pool(tmp_path: Path):
    """A real secrets file wired through the real SAC_SECRETS_ENVRC seam."""
    secrets = tmp_path / "secrets"
    secrets.write_text(f"export GITHUB_TOKEN={_FAKE_TOKEN}\n", encoding="utf-8")
    key = "SAC_SECRETS_ENVRC"
    saved = os.environ.get(key)
    os.environ[key] = str(secrets)
    try:
        yield secrets
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def empty_pool(tmp_path: Path):
    """A resolvable but token-free secrets file — the WARN path."""
    secrets = tmp_path / "empty-secrets"
    secrets.write_text("# no token here\n", encoding="utf-8")
    key = "SAC_SECRETS_ENVRC"
    saved_envrc = os.environ.get(key)
    saved_tokens = {
        k: os.environ.pop(k) for k in ("GITHUB_TOKEN", "GH_TOKEN") if k in os.environ
    }
    os.environ[key] = str(secrets)
    try:
        yield secrets
    finally:
        if saved_envrc is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved_envrc
        os.environ.update(saved_tokens)


def test_token_is_written_into_the_agent_env_file(tmp_path: Path, secrets_pool):
    # Arrange
    dest = tmp_path / "to_home"
    dest.mkdir()
    # Act
    ensure_github_token(_Config(), dest)
    # Assert
    assert f"GITHUB_TOKEN={_FAKE_TOKEN}" in (dest / ".env").read_text()


def test_gh_alias_is_written_too(tmp_path: Path, secrets_pool):
    # Arrange: gh prefers GH_TOKEN while fleet scripts read GITHUB_TOKEN.
    # Writing one and not the other is how an agent ends up holding a token
    # the tool it needs cannot see.
    dest = tmp_path / "to_home"
    dest.mkdir()
    # Act
    ensure_github_token(_Config(), dest)
    # Assert
    assert f"GH_TOKEN={_FAKE_TOKEN}" in (dest / ".env").read_text()


def test_hand_authored_token_is_left_untouched(tmp_path: Path, secrets_pool):
    # Arrange: an explicit .envrc mapping is authoritative, exactly as for
    # CCT_BOT_TOKEN — the pool must never overwrite it.
    dest = tmp_path / "to_home"
    dest.mkdir()
    (dest / ".env").write_text("GITHUB_TOKEN=hand-authored\n", encoding="utf-8")
    # Act
    ensure_github_token(_Config(), dest)
    # Assert
    assert "GITHUB_TOKEN=hand-authored" in (dest / ".env").read_text()


def test_missing_token_does_not_raise(tmp_path: Path, empty_pool):
    # Arrange: a missing token degrades PR creation only. It must NEVER fail
    # the boot — an agent without one still does useful work.
    dest = tmp_path / "to_home"
    dest.mkdir()
    # Act
    ensure_github_token(_Config(), dest)
    # Assert
    assert dest.is_dir()


def test_missing_token_writes_no_empty_value(tmp_path: Path, empty_pool):
    # Arrange: an EMPTY GITHUB_TOKEN is worse than none — it is exactly what
    # made this incident hard to read, because `[ -n "$VAR" ]` and "is the
    # name defined" disagree, and env-check reported it as "set".
    dest = tmp_path / "to_home"
    dest.mkdir()
    # Act
    ensure_github_token(_Config(), dest)
    # Assert
    assert not (dest / ".env").is_file() or "GITHUB_TOKEN=" not in (
        dest / ".env"
    ).read_text()
