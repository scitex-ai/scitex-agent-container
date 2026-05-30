"""Tests for ``sac accounts refresh --all --skip-active``.

The active account is identified by ``~/.claude.json``'s
``oauthAccount.emailAddress`` (the same field ``sac accounts list`` /
``sync-live`` key off), matched case-insensitively against each stored
account's ``email_address``. ``--skip-active`` excludes that account from
an ``--all`` refresh so the in-use refresh_token is never rotated.

No-mocks (PA-306): real on-disk credentials + real account-store layout
via ``$HOME`` redirection; HTTP injected at the ``urllib.request.urlopen``
boundary.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _seed_account(home: Path, name: str, *, email: str) -> Path:
    save_account(name, {"email_address": email}, home=home)
    creds = _store_dir(home) / name / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "refreshToken": "the-refresh",
                    "clientId": "cid",
                }
            }
        )
    )
    return creds


def _set_active_login(home: Path, *, email: str) -> None:
    """Write ``~/.claude.json`` so the active-account email resolves."""
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


@pytest.fixture
def opener_swap() -> Iterator[dict]:
    import urllib.request

    state: dict[str, Any] = {"response": {"access_token": "NEW", "expires_in": 3600}}
    saved = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        return _FakeResp(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        yield state
    finally:
        urllib.request.urlopen = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# --skip-active excludes the matching stored account
# ---------------------------------------------------------------------------


def test_skip_active_does_not_rotate_active_account(sandbox_home, opener_swap) -> None:
    # Arrange — alpha is the active login; beta is idle.
    creds_a = _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _seed_account(sandbox_home, "beta", email="beta@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — the active account's token is left untouched.
    written = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "OLD-ACCESS"


def test_skip_active_still_rotates_idle_account(sandbox_home, opener_swap) -> None:
    # Arrange — alpha active, beta idle.
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    creds_b = _seed_account(sandbox_home, "beta", email="beta@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — the idle account IS refreshed.
    written = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_skip_active_reports_excluded_name(sandbox_home, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _seed_account(sandbox_home, "beta", email="beta@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — operator is told which account was excluded.
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "alpha" in text and "skip-active" in text


def test_skip_active_match_is_case_insensitive(sandbox_home, opener_swap) -> None:
    # Arrange — stored email differs only in case from the live login.
    creds_a = _seed_account(sandbox_home, "alpha", email="Alpha@X.IO")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — case-folded email still matches → active account skipped.
    written = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "OLD-ACCESS"


def test_skip_active_unresolvable_skips_nothing(sandbox_home, opener_swap) -> None:
    # Arrange — no ~/.claude.json → active email cannot be resolved.
    creds_a = _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — nothing is excluded; every account is refreshed.
    written = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_skip_active_unresolvable_logs_it(sandbox_home, opener_swap) -> None:
    # Arrange — no active login resolvable.
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert — the no-active case is surfaced, not silent.
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "no active account resolvable" in text


def test_without_skip_active_rotates_active_account(sandbox_home, opener_swap) -> None:
    # Arrange — same setup, but WITHOUT --skip-active.
    creds_a = _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — behaviour is unchanged without the flag: active IS rotated.
    written = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"
