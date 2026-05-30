"""Tests for ``sac accounts refresh`` CLI command.

No-mocks (PA-306): real on-disk credentials + real account-store layout
via ``$HOME`` redirection. HTTP is injected at the
``urllib.request.urlopen`` boundary via a fixture-managed module-level
swap (treating that callable as the production HTTP seam, NOT a mock).

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


# ---------------------------------------------------------------------------
# Sandbox + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _seed_account(
    home: Path,
    name: str,
    *,
    refresh: str | None = "the-refresh",
    client_id: str | None = "cid",
) -> Path:
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = _store_dir(home) / name / ".credentials.json"
    oauth: dict[str, Any] = {"accessToken": "OLD-ACCESS"}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if client_id is not None:
        oauth["clientId"] = client_id
    creds.write_text(json.dumps({"claudeAiOauth": oauth}))
    return creds


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
    """Swap ``urllib.request.urlopen`` at the production boundary so
    ``refresh_account_credentials`` reaches a deterministic endpoint
    without a real network call. Returns a state dict the test can
    program (``{"response": <dict|Exception>}``).
    """
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
# Single-name happy path
# ---------------------------------------------------------------------------


def test_account_refresh_single_writes_new_token_to_same_file(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    creds = _seed_account(sandbox_home, "work")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "work"])
    # Assert — atomic write-back lands in the SAME per-account creds file.
    written = json.loads(creds.read_text())
    assert written["claudeAiOauth"]["accessToken"] == "NEW"


def test_account_refresh_single_exits_zero_on_success(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work"])
    # Assert
    assert result.exit_code == 0


def test_account_refresh_single_prints_new_expiry(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work"])
    # Assert
    assert "new expiry" in result.output


def test_account_refresh_single_does_not_print_token_value(
    sandbox_home, opener_swap
) -> None:
    # Arrange — seed with a recognisable old token + program a recognisable new one
    creds = _seed_account(sandbox_home, "work")
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "ORIGINAL-ACCESS-XYZ",
                    "refreshToken": "ORIGINAL-REFRESH-XYZ",
                    "clientId": "cid",
                }
            }
        )
    )
    opener_swap["response"] = {"access_token": "BRAND-NEW-XYZ", "expires_in": 3600}
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work"])
    # Assert — neither old nor new token bytes ever surface to stdout/stderr.
    leaks = ("ORIGINAL-ACCESS-XYZ", "ORIGINAL-REFRESH-XYZ", "BRAND-NEW-XYZ")
    assert not any(leak in (result.output or "") for leak in leaks)


# ---------------------------------------------------------------------------
# --all over multiple accounts
# ---------------------------------------------------------------------------


def test_account_refresh_all_writes_back_to_every_stored_account(
    sandbox_home, opener_swap
) -> None:
    # Arrange — two stored accounts.
    creds_a = _seed_account(sandbox_home, "alpha")
    creds_b = _seed_account(sandbox_home, "beta")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — both per-account files received the new token.
    a = json.loads(creds_a.read_text())["claudeAiOauth"]["accessToken"]
    b = json.loads(creds_b.read_text())["claudeAiOauth"]["accessToken"]
    assert a == "NEW" and b == "NEW"


def test_account_refresh_all_continues_past_failed_account(
    sandbox_home, opener_swap
) -> None:
    # Arrange — alpha has no refresh_token (will fail); beta is healthy.
    _seed_account(sandbox_home, "alpha", refresh=None)
    creds_b = _seed_account(sandbox_home, "beta")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — beta's token was still rotated even though alpha failed first.
    written = json.loads(creds_b.read_text())
    assert written["claudeAiOauth"]["accessToken"] == "NEW"


def test_account_refresh_all_exits_zero_when_any_account_succeeds(
    sandbox_home, opener_swap
) -> None:
    # Arrange — one failing + one succeeding account.
    _seed_account(sandbox_home, "alpha", refresh=None)
    _seed_account(sandbox_home, "beta")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all"])
    # Assert — partial success is success.
    assert result.exit_code == 0


def test_account_refresh_all_exits_nonzero_when_every_account_fails(
    sandbox_home, opener_swap
) -> None:
    # Arrange — both accounts lack refresh_token.
    _seed_account(sandbox_home, "alpha", refresh=None)
    _seed_account(sandbox_home, "beta", refresh=None)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all"])
    # Assert
    assert result.exit_code != 0


def test_account_refresh_all_failure_messages_mention_claude_login(
    sandbox_home, opener_swap
) -> None:
    # Arrange — account missing the refresh_token.
    _seed_account(sandbox_home, "alpha", refresh=None)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all"])
    # Assert — operator is told the recovery path (a real /login) is needed.
    # click >=8.2 separates stderr; <8.2 merges it. Tolerate both.
    stderr_text = getattr(result, "stderr", "") or ""
    assert "claude /login" in ((result.output or "") + stderr_text)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_account_refresh_json_emits_one_entry_per_account(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "alpha")
    _seed_account(sandbox_home, "beta")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert len(payload) == 2


def test_account_refresh_json_carries_success_flag(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload[0]["success"] is True


def test_account_refresh_json_omits_token_values(
    sandbox_home, opener_swap
) -> None:
    # Arrange — recognisable tokens on both sides.
    creds = _seed_account(sandbox_home, "work")
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "ORIGINAL-ACCESS-XYZ",
                    "refreshToken": "ORIGINAL-REFRESH-XYZ",
                    "clientId": "cid",
                }
            }
        )
    )
    opener_swap["response"] = {"access_token": "BRAND-NEW-XYZ", "expires_in": 3600}
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work", "--json"])
    # Assert — token bytes (old or new) never appear in the JSON payload.
    leaks = ("ORIGINAL-ACCESS-XYZ", "ORIGINAL-REFRESH-XYZ", "BRAND-NEW-XYZ")
    assert not any(leak in (result.output or "") for leak in leaks)


# ---------------------------------------------------------------------------
# Argument plumbing
# ---------------------------------------------------------------------------


def test_account_refresh_no_args_exits_nonzero(sandbox_home, opener_swap) -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh"])
    # Assert — must specify a name OR --all.
    assert result.exit_code != 0


def test_account_refresh_name_and_all_together_exits_nonzero(
    sandbox_home, opener_swap
) -> None:
    # Arrange
    _seed_account(sandbox_home, "work")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "work", "--all"])
    # Assert
    assert result.exit_code != 0
