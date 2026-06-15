"""Tests for ``spec.claude.effort`` → runner argv + settings.json wiring.

Two carriers:
  * TUI runner — passes ``--effort <level>`` to the bundled ``claude``
    binary (2.1.150 in sac-base SIF supports the flag).
  * SDK runner — materialises ``effortLevel: <level>`` into the agent's
    ``.claude/settings.local.json`` (the SDK reads it from there).

Operator directive 2026-06-15: surface this knob fleet-wide so every
agent can run at effort=max. Empty / missing means "no override".

TQ cleanup: docstring (TQ001), AAA markers (TQ002), descriptive names
(TQ003), one assertion per test (TQ007), no monkeypatch parameter
(env save/restore where needed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes._apptainer_inner_argv import _tui_runner_argv
from scitex_agent_container.runtimes.settings_json import (
    _MANAGED_KEYS,
    cleanup_settings_json,
    setup_settings_json,
)


def _settings_path(workdir) -> Path:
    return Path(workdir) / ".claude" / "settings.local.json"


# ---------------------------------------------------------------------------
# TUI runner: --effort wiring
# ---------------------------------------------------------------------------


def test_tui_runner_omits_effort_flag_when_unset():
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort=""))
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--effort" not in argv


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_tui_runner_emits_effort_flag_when_set(level):
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort=level))
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--effort" in argv


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_tui_runner_effort_value_follows_flag(level):
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort=level))
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert argv[argv.index("--effort") + 1] == level


def test_tui_runner_effort_appears_alongside_model_flag():
    # Arrange — both knobs set; both must reach the binary.
    cfg = AgentConfig(
        name="t",
        claude=ClaudeSpec(model="opus", effort="max"),
    )
    # Act
    argv = _tui_runner_argv(cfg)
    # Assert
    assert "--model" in argv and "--effort" in argv


# ---------------------------------------------------------------------------
# SDK settings.json: effortLevel wiring
# ---------------------------------------------------------------------------


def test_settings_json_omits_effort_level_when_unset(tmp_path):
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort=""))
    # Act
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(_settings_path(tmp_path).read_text())
    # Assert
    assert "effortLevel" not in data


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_settings_json_writes_effort_level_when_set(tmp_path, level):
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort=level))
    # Act
    setup_settings_json(cfg, str(tmp_path))
    data = json.loads(_settings_path(tmp_path).read_text())
    # Assert
    assert data["effortLevel"] == level


def test_managed_keys_includes_effort_level():
    # Arrange
    # Act
    has_key = "effortLevel" in _MANAGED_KEYS
    # Assert
    assert has_key


def test_cleanup_removes_effort_level(tmp_path):
    # Arrange
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort="max"))
    setup_settings_json(cfg, str(tmp_path))
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    remaining = (
        json.loads(_settings_path(tmp_path).read_text())
        if _settings_path(tmp_path).exists()
        else {}
    )
    assert "effortLevel" not in remaining


def test_cleanup_preserves_user_keys_alongside_effort(tmp_path):
    # Arrange
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"userCustomKey": "keep"}))
    cfg = AgentConfig(name="t", claude=ClaudeSpec(effort="max"))
    setup_settings_json(cfg, str(tmp_path))
    # Act
    cleanup_settings_json(cfg, str(tmp_path))
    # Assert
    assert json.loads(sp.read_text()) == {"userCustomKey": "keep"}
