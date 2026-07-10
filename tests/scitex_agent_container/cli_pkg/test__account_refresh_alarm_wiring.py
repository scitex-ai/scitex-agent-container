"""End-to-end wiring: ``sac accounts refresh`` fires the loud failure alarm.

INCIDENT 2026-07-10: refresh failures reached only the journal; nothing
paged the operator. These tests drive the REAL CLI against a real
on-disk account store whose credentials lack a refresh_token (a genuine
no-network failure path) and verify the alarm chain runs: the lead rail
is unconfigured in the sandbox, so delivery falls through to the
scitex-todo help card written into a sandboxed shared store — exactly
the production fallback order.

No-mocks (PA-306): real store, real CLI, real scitex-todo store file.
AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_env(tmp_path, env_save_restore):
    """Isolate HOME + every store-resolution env the alarm chain reads."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set(
        "SCITEX_TODO_TASKS_YAML_SHARED", str(tmp_path / "todo-tasks.yaml")
    )
    env_save_restore.delete("SCITEX_DIR")
    env_save_restore.delete("SAC_NAME")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    return home


def _seed_stale_account_without_refresh_token(home: Path, name: str) -> None:
    """A stale (past-expiry) account whose creds lack a refresh_token.

    The --all gate refreshes it (stale), and the refresh fails BEFORE
    any network call ("no refresh_token in credentials") — a real
    failure result with zero network dependence.
    """
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "expiresAt": int(time.time() * 1000) - 3_600_000,
                }
            }
        )
    )


def test_failed_refresh_prints_alerted_operator_line(
    sandbox_env: Path, tmp_path: Path
) -> None:
    # Arrange
    _seed_stale_account_without_refresh_token(sandbox_env, "stale-acct")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all"])
    # Assert — the alarm ran and reported a delivered alert.
    assert "ALERTED operator" in result.output


def test_failed_refresh_upserts_help_card_into_todo_store(
    sandbox_env: Path, tmp_path: Path
) -> None:
    # Arrange — the sandbox has no lead: block, so delivery falls
    # through to the scitex-todo help-card rail (the production
    # fallback), landing in the sandboxed shared store.
    _seed_stale_account_without_refresh_token(sandbox_env, "stale-acct")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — the canonical BLOCKING-YOU card exists for the account.
    assert "help-accounts-refresh-stale-acct-waiting" in (
        tmp_path / "todo-tasks.yaml"
    ).read_text()


def test_second_run_does_not_realert_for_same_dead_account(
    sandbox_env: Path, tmp_path: Path
) -> None:
    # Arrange — dedupe across CLI invocations rides the state file under
    # the sandbox HOME's runtime dir.
    _seed_stale_account_without_refresh_token(sandbox_env, "stale-acct")
    runner = CliRunner()
    runner.invoke(account, ["refresh", "--all"])
    # Act
    second = runner.invoke(account, ["refresh", "--all"])
    # Assert — quiet on the repeat run.
    assert "ALERTED operator" not in second.output
