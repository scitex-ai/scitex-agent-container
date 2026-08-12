"""Tests for the hook-origin manifest (``runtimes._hook_origin_manifest``).

Covers the reshape from cascade provenance to ``{event: {command: layer}}``
and the runtime write: where it lands, what it records, when it declines to
write, and that it never fails a deploy.

STX-NM002: no mocks / monkeypatch. The runtime dir is redirected by setting
the module's own documented relocation env var in a ``yield`` fixture and
restoring it on teardown, and every filesystem assertion reads real bytes.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container._runtime_paths import RUNTIME_DIR_ENV
from scitex_agent_container.runtimes._hook_origin_manifest import (
    hook_origins,
    manifest_path,
    write_hook_manifest,
)


def _set_runtime_dir(value: str) -> "str | None":
    previous = os.environ.get(RUNTIME_DIR_ENV)
    os.environ[RUNTIME_DIR_ENV] = value
    return previous


def _restore_runtime_dir(previous: "str | None") -> None:
    if previous is None:
        os.environ.pop(RUNTIME_DIR_ENV, None)
    else:
        os.environ[RUNTIME_DIR_ENV] = previous


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Iterator[Path]:
    """Point the runtime base dir at a throwaway tree for one test."""
    previous = _set_runtime_dir(str(tmp_path))
    try:
        yield tmp_path
    finally:
        _restore_runtime_dir(previous)


@pytest.fixture()
def runtime_dir_blocked_by_a_file(tmp_path: Path) -> Iterator[Path]:
    """Runtime base dir points at a REGULAR FILE, so ``mkdir`` cannot succeed."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    previous = _set_runtime_dir(str(blocker))
    try:
        yield blocker
    finally:
        _restore_runtime_dir(previous)


def test_hook_key_is_reshaped_to_event_and_command() -> None:
    # Arrange
    prov = {"hooks.PreToolUse.guard.sh": "user-shared"}
    # Act
    origins = hook_origins(prov)
    # Assert
    assert origins == {"PreToolUse": {"guard.sh": "user-shared"}}


def test_non_hook_keys_are_ignored() -> None:
    # Arrange
    prov = {"statusLine": "per-agent", "_comment": "user-shared"}
    # Act
    origins = hook_origins(prov)
    # Assert
    assert origins == {}


def test_command_containing_dots_survives_intact() -> None:
    # Arrange — the split must be bounded; hook commands are full paths.
    prov = {"hooks.Stop.$HOME/.claude/hooks/stop/a.b.sh": "per-agent"}
    # Act
    origins = hook_origins(prov)
    # Assert
    assert origins["Stop"] == {"$HOME/.claude/hooks/stop/a.b.sh": "per-agent"}


def test_bare_hooks_key_is_not_mistaken_for_a_command() -> None:
    # Arrange — the pre-fix sentinel wrote this shape; it names no command.
    prov = {"hooks": "(merged)"}
    # Act
    origins = hook_origins(prov)
    # Assert
    assert origins == {}


def test_no_hooks_writes_no_file(runtime_dir: Path) -> None:
    # Arrange
    prov = {"statusLine": "per-agent"}
    # Act
    written = write_hook_manifest("agent-a", prov)
    # Assert
    assert written is None


def test_manifest_lands_under_runtime_logs_keyed_by_agent(runtime_dir: Path) -> None:
    # Arrange
    prov = {"hooks.PreToolUse.guard.sh": "user-shared"}
    # Act
    written = write_hook_manifest("agent-a", prov)
    # Assert
    assert written == runtime_dir / "logs" / "agent-a" / "hook-origins.json"


def test_manifest_records_the_layer_that_armed_each_hook(runtime_dir: Path) -> None:
    # Arrange
    prov = {
        "hooks.PreToolUse.a.sh": "user-shared",
        "hooks.PreToolUse.b.sh": "per-agent",
    }
    # Act
    write_hook_manifest("agent-a", prov)
    payload = json.loads(manifest_path("agent-a").read_text())
    # Assert
    assert payload["events"]["PreToolUse"] == {
        "a.sh": "user-shared",
        "b.sh": "per-agent",
    }


def test_manifest_counts_every_armed_hook(runtime_dir: Path) -> None:
    # Arrange
    prov = {
        "hooks.PreToolUse.a.sh": "user-shared",
        "hooks.Stop.b.sh": "per-agent",
    }
    # Act
    write_hook_manifest("agent-a", prov)
    payload = json.loads(manifest_path("agent-a").read_text())
    # Assert
    assert payload["hook_count"] == 2


def test_manifest_lists_the_contributing_layers(runtime_dir: Path) -> None:
    # Arrange
    prov = {
        "hooks.PreToolUse.a.sh": "user-shared",
        "hooks.Stop.b.sh": "per-agent",
    }
    # Act
    write_hook_manifest("agent-a", prov)
    payload = json.loads(manifest_path("agent-a").read_text())
    # Assert
    assert payload["layers"] == ["per-agent", "user-shared"]


def test_unwritable_runtime_dir_does_not_raise(
    runtime_dir_blocked_by_a_file: Path,
) -> None:
    # Arrange
    prov = {"hooks.PreToolUse.a.sh": "user-shared"}
    # Act
    written = write_hook_manifest("agent-a", prov)
    # Assert
    assert written is None
