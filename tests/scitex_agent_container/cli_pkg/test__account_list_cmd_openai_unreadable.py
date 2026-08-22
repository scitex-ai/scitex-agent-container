"""``sac accounts list`` must survive an UNREADABLE OpenAI store.

MEASURED, 2026-07-19 (audit of PR #782). `_account_list_cmd.account_list` called
`read_codex_accounts_metadata()` unguarded, on the line ABOVE both the `--json`
branch and the Claude credential block. That function raises
`CodexAccountSyncError` — a bare `RuntimeError`, not a `ClickException`, so click
prints a traceback and exits 1 — for every degraded state of the OpenAI store
once its root directory exists. `sac accounts sync-openai` creates that root
permanently, and it already exists on the operator's host.

So an OpenAI-side problem deleted the CLAUDE credential view: no table, no TTLs,
no usage bars, and a non-zero exit for anything parsing `--json`. That is the
operator's primary triage instrument, and it is reached most often DURING a
credential incident — which is precisely when a store is half-written, revoked,
or logged out. The Claude reads on the same code path were always tolerant
(`try/except (OSError, JSONDecodeError)` and a `list_accounts` documented "Never
raises"); the new provider axis silently lowered that contract.

The states that raise, all of them ordinary rather than exotic: store root
present but holding no `*/auth.json` (log out of ChatGPT, or delete one stale
credential); `auth.json` rewritten to `{}`; a truncated write; a file owned by
another uid; and an api-key-mode `auth.json` — a LEGITIMATE auth mode — because
it carries no decodable JWT claims. One broken member of a healthy pool raises
for the whole pool.

DEGRADE TO A THIRD STATE, NOT TO ABSENCE. An absent store already returns `[]`
and renders correctly as "no OpenAI accounts". If an unreadable store also
rendered as `[]`, the command would tell the operator their store is EMPTY when
it is BROKEN — trading a loud failure for a quiet wrong answer, which is the
worse of the two. `openai_error` is therefore the distinguishing fact: absent is
`[]` with `openai_error: null`, unreadable is `[]` with the message.

Each test here is paired with the control that gives it meaning: the same
scenario against a HEALTHY store must NOT report an error, or an assertion on a
degraded render would pass for a reason that has nothing to do with the guard.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path: Path, env_save_restore) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("CODEX_HOME")
    env_save_restore.delete("SCITEX_GENAI_CODEX_HOMES")
    return home


def _write_healthy_codex_auth(home: Path) -> None:
    """A store the reader accepts — the control arm."""
    claims = {
        "email": "person@example.com",
        "name": "Person Example",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "openai-account",
            "chatgpt_plan_type": "plus",
        },
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": f"header.{payload.rstrip('=')}.signature",
            "refresh_token": "refresh-must-not-appear",
        },
        "last_refresh": "2026-07-01T00:00:00Z",
    }
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(auth))


def _write_logged_out_codex_auth(home: Path) -> None:
    """The state a ChatGPT logout leaves behind: present, structurally empty.

    The STORE ROOT is what arms the raise, not the auth file. Both raise sites
    are gated on ``configured or stored``, where ``stored`` is
    ``_openai_store_root(home).exists()`` — and `sac accounts sync-openai`
    creates that root permanently on first use. A `{}` auth.json with no store
    root degrades gracefully instead, which is why an earlier version of this
    fixture passed against the UNGUARDED code and proved nothing.
    """
    store = home / ".scitex" / "agent-container" / "accounts" / "openai"
    path = store / "person-example-com" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")


def test_list_exits_zero_when_the_openai_store_is_unreadable():
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert result.exit_code == 0, result.output


def test_list_json_exits_zero_when_the_openai_store_is_unreadable():
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_list_json_stays_parseable_when_the_openai_store_is_unreadable():
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert isinstance(json.loads(result.stdout), dict), result.output


def test_list_json_reports_the_unreadable_store_as_an_error_not_as_emptiness():
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert json.loads(result.stdout)["openai_error"] is not None, result.output


def test_list_human_output_names_the_store_as_unreadable():
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "UNREADABLE" in result.output, result.output


# --------------------------------------------------------------------------
# Controls. Without these, the assertions above could pass for reasons that
# have nothing to do with the guard.
# --------------------------------------------------------------------------


def test_control_healthy_store_reports_no_error():
    """A readable store must NOT be flagged, or the flag means nothing."""
    # Arrange
    runner = CliRunner()
    _write_healthy_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert json.loads(result.stdout)["openai_error"] is None, result.output


def test_control_absent_store_is_not_reported_as_unreadable():
    """Absent and unreadable are different facts and must render differently."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert json.loads(result.stdout)["openai_error"] is None, result.output


def test_control_absent_store_still_exits_zero():
    """The pre-existing graceful path must not regress."""
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_control_healthy_store_still_lists_the_openai_account():
    """The guard must not suppress accounts that ARE readable."""
    # Arrange
    runner = CliRunner()
    _write_healthy_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert json.loads(result.stdout)["openai_accounts"], result.output


def test_control_unreadable_store_does_not_leak_the_claude_section():
    """The whole point: the Claude view survives an OpenAI-side failure."""
    # Arrange
    runner = CliRunner()
    _write_logged_out_codex_auth(Path.home())
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert "stored" in json.loads(result.stdout), result.output
