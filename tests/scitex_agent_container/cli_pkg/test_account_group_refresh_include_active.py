"""Tests for ``sac accounts refresh --all --include-active``.

Master-host single-refresher model (operator 2026-07-08, credential-churn
root-cause fix): agents bind the credential ``:ro`` and NEVER refresh it,
so the host-side ``sac-accounts-refresh`` timer becomes the SOLE refresher
and MUST refresh the active account too — otherwise the active account's
agents die when its access_token expires. ``--include-active`` makes that
intent explicit and forces every stored account (active + pinned-running
included) through the refresh, writing the fresh access_token back to each
account's snapshot file (the file agents ``:ro``-read).

``--include-active`` is mutually exclusive with ``--skip-active``.

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
# --include-active refreshes the active account (the whole point)
# ---------------------------------------------------------------------------


def test_include_active_rotates_active_account(sandbox_home, opener_swap) -> None:
    # Arrange — alpha is the active login. Under the :ro model nobody
    # else refreshes it, so the timer's --include-active run must.
    creds_a = _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _seed_account(sandbox_home, "beta", email="beta@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — the active account's token IS rotated (written to snapshot).
    written = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_include_active_writes_fresh_token_to_snapshot_file(
    sandbox_home, opener_swap
) -> None:
    # Arrange — the snapshot file agents :ro-read must carry the fresh
    # access_token after the refresh (this is the load-bearing invariant:
    # the timer's refresh must update the file agents consume).
    creds_a = _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — the snapshot at the canonical account-store path was updated.
    snapshot = _store_dir(sandbox_home) / "alpha" / ".credentials.json"
    written = json.loads(snapshot.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_include_active_also_rotates_idle_account(sandbox_home, opener_swap) -> None:
    # Arrange — alpha active, beta idle; --include-active refreshes both.
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    creds_b = _seed_account(sandbox_home, "beta", email="beta@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — the idle account IS refreshed too.
    written = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_include_active_reports_intent(sandbox_home, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — the operator is told the single-refresher intent.
    text = (result.output or "") + (getattr(result, "stderr", "") or "")
    assert "include-active" in text


def test_include_active_conflicts_with_skip_active(sandbox_home, opener_swap) -> None:
    # Arrange — the two flags express opposite intents; combining them is
    # a usage error (exit 2), not a silent last-wins surprise.
    _seed_account(sandbox_home, "alpha", email="alpha@x.io")
    _set_active_login(sandbox_home, email="alpha@x.io")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        account, ["refresh", "--all", "--include-active", "--skip-active"]
    )
    # Assert — non-zero exit; the flags are mutually exclusive.
    assert result.exit_code == 2
