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


@pytest.fixture(params=[False, True])
def preview_store(pause_store, request):
    """Prepare either an active account or an existing pause before previewing."""
    group, env, store = pause_store
    runner = CliRunner()
    if request.param:
        result = runner.invoke(group, ["pause", "alpha", "--reason", "rest"], env=env)
        if result.exit_code:
            pytest.fail(result.output)
    return group, env, store


def test_preview_preserves_account_and_pause_bytes(preview_store):
    # Arrange
    group, env, store = preview_store
    before = _snapshot(store)
    # Act
    result = CliRunner().invoke(
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


@pytest.fixture(
    params=[
        ("alpha", "rest", True, False, 1),
        ("alpha", "rest", True, True, 0),
        ("unknown", "rest", False, False, 1),
        ("alpha", "   ", False, False, 1),
    ],
    ids=["last-valid-refused", "last-valid-explicit", "unknown", "blank-reason"],
)
def preview_boundary(pause_store, request):
    """Prepare real store states and explicit CLI arguments for each boundary."""
    group, env, store = pause_store
    name, reason, last_valid, yes, expected_exit = request.param
    if last_valid:
        path = store / "beta" / ".credentials.json"
        payload = json.loads(path.read_text())
        payload["claudeAiOauth"]["expiresAt"] = 1
        path.write_text(json.dumps(payload))
    args = ["pause", name, "--reason", reason, "--dry-run"]
    if yes:
        args.append("--yes")
    return group, env, store, args, expected_exit


def test_preview_keeps_existing_refusal_boundaries(preview_boundary):
    # Arrange
    group, env, store, args, expected_exit = preview_boundary
    before = _snapshot(store)
    # Act
    result = CliRunner().invoke(group, args, env=env)
    # Assert
    assert (result.exit_code, _snapshot(store)) == (expected_exit, before)


@pytest.fixture(
    params=[
        ("active", "alpha", 0, "would resume alpha"),
        ("corrupt", "alpha", 0, "unusable pause record"),
        ("absent", "alpha", 0, "nothing to lift"),
        ("active", "unknown", 1, "unknown account"),
        ("active", "..", 1, "unknown account"),
    ],
    ids=["paused", "corrupt-record", "not-paused", "unknown", "parent-path"],
)
def resume_preview_store(pause_store, request):
    """Prepare real pause records and preserve all local store bytes."""
    group, env, store = pause_store
    state, name, expected_exit, expected_text = request.param
    if state == "active":
        result = CliRunner().invoke(
            group, ["pause", "alpha", "--reason", "subscription rest"], env=env
        )
        if result.exit_code:
            pytest.fail(result.output)
    elif state == "corrupt":
        (store / "alpha" / "pause.json").write_text("not valid JSON")
    return group, env, store, name, expected_exit, expected_text


def test_resume_preview_reports_the_record_without_changing_store(resume_preview_store):
    # Arrange
    group, env, store, name, expected_exit, expected_text = resume_preview_store
    before = _snapshot(store)
    # Act
    result = CliRunner().invoke(group, ["resume", name, "--dry-run"], env=env)
    # Assert
    assert (
        result.exit_code,
        expected_text in result.output,
        "health now:" in result.output,
        _snapshot(store),
    ) == (expected_exit, True, False, before)
