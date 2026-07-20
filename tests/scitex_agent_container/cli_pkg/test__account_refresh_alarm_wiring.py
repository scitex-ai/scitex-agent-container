"""End-to-end wiring: ``sac accounts refresh`` fires the loud failure alarm.

INCIDENT 2026-07-10: refresh failures reached only the journal; nothing
paged the operator. These tests drive the REAL CLI against a real
on-disk account store whose credentials lack a refresh_token (a genuine
no-network failure path) and verify the alarm chain runs end to end.

The chain's first leg is the DURABLE RECORD in sac's own event log, and
it is the leg that decides the outcome: the lead ``blocker`` push on top
is best-effort and is unconfigured in this sandbox (no ``lead:`` block),
which must NOT stop the account being marked alerted. That is the whole
point of the split — a fleet with nowhere to push must not re-page on
every one of the timer's ~12 daily runs.

No-mocks (PA-306), no monkeypatching: real account store, real CLI, a
real temp JSONL event log redirected through the documented
``SAC_EVENT_LOG`` env var. AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._events import (
    EVENT_LOG_ENV,
    SUBJECT_DEGRADED,
    read_events,
)
from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_env(tmp_path, env_save_restore):
    """Isolate HOME + every path-resolution env the alarm chain reads."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set(EVENT_LOG_ENV, str(tmp_path / "sac-events.jsonl"))
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


def test_failed_refresh_records_the_account_in_the_event_log(
    sandbox_env: Path, tmp_path: Path
) -> None:
    # Arrange — the sandbox has no ``lead:`` block, so the push leg fails
    # for real. The DURABLE RECORD is what must survive that, because it
    # is the only leg that owes nothing to another host being reachable.
    _seed_stale_account_without_refresh_token(sandbox_env, "stale-acct")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert
    degraded = read_events(
        tmp_path / "sac-events.jsonl",
        subsystem="accounts-refresh",
        event=SUBJECT_DEGRADED,
    )
    assert [e.subject for e in degraded] == ["stale-acct"]


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
