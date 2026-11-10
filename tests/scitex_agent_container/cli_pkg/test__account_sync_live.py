"""Live-credential snapshots require approval, including scheduled poll calls."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container._account import creds_sync
from scitex_agent_container.cli_pkg._main import main


@pytest.fixture
def owned_home(tmp_path, env_save_restore):
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("SCITEX_DIR", str(tmp_path / ".scitex"))
    return tmp_path


@pytest.fixture
def backend_trap():
    calls = []
    previous = creds_sync.sync_live

    def refuse():
        calls.append("called")
        raise AssertionError("preview/refusal must not read credentials")

    creds_sync.sync_live = refuse
    try:
        yield calls
    finally:
        creds_sync.sync_live = previous


@pytest.mark.parametrize("flags", [[], ["--poll"], ["--json"], ["--poll", "--json"]])
def test_live_sync_requires_approval_before_backend(owned_home, backend_trap, flags):
    # Arrange
    store = owned_home / ".scitex/agent-container/accounts"
    # Act
    result = CliRunner().invoke(main, ["accounts", "sync-live", *flags])
    # Assert
    assert (
        result.exit_code,
        "--yes" in result.output,
        backend_trap,
        store.exists(),
    ) == (2, True, [], False)


@pytest.mark.parametrize(
    "flags", [["--dry-run"], ["--dry-run", "--yes"], ["-y", "--dry-run"]]
)
@pytest.mark.parametrize("poll", [[], ["--poll"]])
def test_live_preview_uses_no_backend(owned_home, backend_trap, flags, poll):
    # Arrange
    store = owned_home / ".scitex/agent-container/accounts"
    # Act
    result = CliRunner().invoke(main, ["accounts", "sync-live", *flags, *poll])
    # Assert
    assert (result.exit_code, backend_trap, store.exists()) == (0, [], False)


def test_poll_preview_reports_only_arguments(owned_home, backend_trap):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        main, ["--json", "accounts", "sync-live", "--poll", "--dry-run"]
    )
    # Assert
    assert json.loads(result.stdout) == {
        "action": "sync-live",
        "dry_run": True,
        "poll": True,
    }


@pytest.mark.parametrize(
    "json_flags",
    [["--json", "accounts", "sync-live"], ["accounts", "sync-live", "--json"]],
)
def test_approved_poll_keeps_absent_login_noop_and_json(owned_home, json_flags):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, [*json_flags, "--poll", "--yes"])
    # Assert
    assert (result.exit_code, json.loads(result.stdout)["action"]) == (
        0,
        "live-cred-invalid",
    )
