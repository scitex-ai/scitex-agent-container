"""Pause previews validate the real local account store without writing it."""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_pause import register_pause_commands


@pytest.fixture
def pause_store(tmp_path: Path):
    """Use fresh fake credentials; no provider calls or shared state."""
    store = tmp_path / ".scitex" / "agent-container" / "accounts"
    for name in ("alpha", "beta"):
        account = store / name
        account.mkdir(parents=True)
        (account / "account.json").write_text(json.dumps({"name": name}))
        (account / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "not-a-real-token",
                        "refreshToken": "not-a-real-refresh-token",
                        "expiresAt": int((time.time() + 28800) * 1000),
                    }
                }
            )
        )

    @click.group()
    def group():
        """Own the command registration used by the actual CLI."""

    register_pause_commands(group)
    env = {"HOME": str(tmp_path), "SCITEX_DIR": str(tmp_path / ".scitex")}
    return group, env, store


def _snapshot(store: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(store)): path.read_bytes()
        for path in store.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("existing_pause", [False, True])
def test_preview_preserves_account_and_pause_bytes(pause_store, existing_pause):
    # Arrange
    group, env, store = pause_store
    runner = CliRunner()
    if existing_pause:
        result = runner.invoke(group, ["pause", "alpha", "--reason", "rest"], env=env)
        if result.exit_code:
            pytest.fail(result.output)
    before = _snapshot(store)
    # Act
    result = runner.invoke(
        group, ["pause", "alpha", "--reason", "new reason", "--dry-run"], env=env
    )
    # Assert
    assert (
        result.exit_code,
        "would pause alpha" in result.output,
        _snapshot(store),
    ) == (
        0,
        True,
        before,
    )


@pytest.mark.parametrize(
    "name,reason,last_valid,yes,expected_exit",
    [
        ("alpha", "rest", True, False, 1),
        ("alpha", "rest", True, True, 0),
        ("unknown", "rest", False, False, 1),
        ("alpha", "   ", False, False, 1),
    ],
)
def test_preview_keeps_existing_refusal_boundaries(
    pause_store, name, reason, last_valid, yes, expected_exit
):
    # Arrange
    group, env, store = pause_store
    if last_valid:
        path = store / "beta" / ".credentials.json"
        payload = json.loads(path.read_text())
        payload["claudeAiOauth"]["expiresAt"] = 1
        path.write_text(json.dumps(payload))
    before = _snapshot(store)
    args = ["pause", name, "--reason", reason, "--dry-run"]
    if yes:
        args.append("--yes")
    # Act
    result = CliRunner().invoke(group, args, env=env)
    # Assert
    assert (result.exit_code, _snapshot(store)) == (expected_exit, before)
