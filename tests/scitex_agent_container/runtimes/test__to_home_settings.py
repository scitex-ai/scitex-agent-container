"""Tests for the layered settings.json deploy cascade (ADR-0018).

Covers ``_to_home_settings.deploy_settings_cascade``: each layer's
``.claude/settings.json`` (or legacy ``settings.local.json``) is deep-merged
into ``dest/.claude/settings.json`` lowest-precedence-first, raising on a
cross-layer scalar conflict and on unparseable JSON.

STX-NM002: no mocks/monkeypatch — real files under ``tmp_path``.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._to_home_errors import (
    LayerMergeConflict,
    WorkspaceSettingsMergeError,
)
from scitex_agent_container.runtimes._to_home_settings import deploy_settings_cascade


def _write_settings(
    layer_dir: Path, payload: dict, *, name: str = "settings.json"
) -> None:
    claude = layer_dir / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / name).write_text(json.dumps(payload))


def test_no_layer_ships_settings_is_noop(tmp_path: Path) -> None:
    # Arrange
    dest = tmp_path / "dest"
    # Act
    deploy_settings_cascade(dest, [("user-shared", tmp_path / "missing")])
    # Assert
    assert not (dest / ".claude" / "settings.json").exists()


def test_layers_deep_merge_into_dest(tmp_path: Path) -> None:
    # Arrange
    base = tmp_path / "base"
    agent = tmp_path / "agent"
    _write_settings(base, {"a": 1})
    _write_settings(agent, {"b": 2})
    dest = tmp_path / "dest"
    # Act
    deploy_settings_cascade(dest, [("user-shared", base), ("per-agent", agent)])
    # Assert
    written = json.loads((dest / ".claude" / "settings.json").read_text())
    assert written == {"a": 1, "b": 2}


def test_cross_layer_scalar_conflict_raises(tmp_path: Path) -> None:
    # Arrange
    base = tmp_path / "base"
    agent = tmp_path / "agent"
    _write_settings(base, {"k": "x"})
    _write_settings(agent, {"k": "y"})
    dest = tmp_path / "dest"
    # Act
    # Assert
    with pytest.raises(LayerMergeConflict):
        deploy_settings_cascade(dest, [("user-shared", base), ("per-agent", agent)])


def test_legacy_local_json_name_is_accepted_as_source(tmp_path: Path) -> None:
    # Arrange
    base = tmp_path / "base"
    _write_settings(base, {"a": 1}, name="settings.local.json")
    dest = tmp_path / "dest"
    # Act
    deploy_settings_cascade(dest, [("user-shared", base)])
    # Assert
    written = json.loads((dest / ".claude" / "settings.json").read_text())
    assert written == {"a": 1}


def test_unparseable_layer_raises(tmp_path: Path) -> None:
    # Arrange
    base = tmp_path / "base"
    (base / ".claude").mkdir(parents=True)
    (base / ".claude" / "settings.json").write_text("{not json")
    dest = tmp_path / "dest"
    # Act
    # Assert
    with pytest.raises(WorkspaceSettingsMergeError):
        deploy_settings_cascade(dest, [("user-shared", base)])
