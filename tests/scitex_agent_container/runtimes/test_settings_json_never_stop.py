"""The never-stop Stop hook must reach the settings file agents actually read.

A Stop hook that is never wired in is not a safety mechanism, it is a file.
These tests assert the actuator survives the whole composition path:
``_HOOKS_CONFIG`` → deep-merge with pre-existing baseline hooks → the
written ``.claude/settings.json``.

PA-306 no-mocks: ``setup_settings_json`` writes a real file into
``tmp_path`` and the assertions read that file back.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.runtimes.settings_json import (
    _HOOKS_CONFIG,
    setup_settings_json,
)

#: The command Claude Code must end up invoking on every turn end.
_ACTUATOR = "scitex-agent-container take-next-item"


def _make_cfg(name: str = "test-agent", **kw) -> AgentConfig:
    return AgentConfig(name=name, claude=ClaudeSpec(flags=[]), **kw)


def _stop_commands(settings: dict) -> list[str]:
    cmds: list[str] = []
    for group in settings.get("hooks", {}).get("Stop", []):
        for hook in group.get("hooks", []):
            if isinstance(hook.get("command"), str):
                cmds.append(hook["command"])
    return cmds


def _write_and_read(tmp_path: Path, cfg: AgentConfig | None = None) -> dict:
    setup_settings_json(cfg or _make_cfg(), str(tmp_path), filename="settings.json")
    return json.loads((tmp_path / ".claude" / "settings.json").read_text())


# ---------------------------------------------------------------------------
# the hook is declared
# ---------------------------------------------------------------------------


def test_hooks_config_declares_the_actuator_on_stop():
    # Arrange
    config = _HOOKS_CONFIG
    # Act
    commands = _stop_commands({"hooks": config})
    # Assert
    assert _ACTUATOR in commands


def test_actuator_does_not_displace_the_event_ingest_hook():
    # Arrange
    config = _HOOKS_CONFIG
    # Act
    commands = _stop_commands({"hooks": config})
    # Assert
    assert "scitex-agent-container event ingest stop" in commands


# ---------------------------------------------------------------------------
# the hook survives to the written file
# ---------------------------------------------------------------------------


def test_actuator_reaches_the_written_settings_file(tmp_path: Path):
    # Arrange
    cfg = _make_cfg()
    # Act
    settings = _write_and_read(tmp_path, cfg)
    # Assert
    assert _ACTUATOR in _stop_commands(settings)


def test_actuator_survives_merge_with_a_baseline_stop_gate(tmp_path: Path):
    """A project's own Stop gate must not clobber the actuator, nor vice
    versa — both have to run."""
    # Arrange
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "clew verify --strict"}
                            ],
                        }
                    ]
                }
            }
        )
    )
    # Act
    settings = _write_and_read(tmp_path)
    commands = _stop_commands(settings)
    # Assert
    assert _ACTUATOR in commands and "clew verify --strict" in commands


def test_repeated_materialise_does_not_duplicate_the_actuator(tmp_path: Path):
    # Arrange
    cfg = _make_cfg()
    setup_settings_json(cfg, str(tmp_path), filename="settings.json")
    # Act
    settings = _write_and_read(tmp_path, cfg)
    # Assert
    assert _stop_commands(settings).count(_ACTUATOR) == 1


def test_operator_can_opt_out_via_exclude_hooks(tmp_path: Path):
    """No-Surprise: the operator who SAW the hook can switch it off."""
    # Arrange
    cfg = _make_cfg("opt-out-agent", exclude_hooks=["take-next-item"])
    # Act
    settings = _write_and_read(tmp_path, cfg)
    # Assert
    assert _ACTUATOR not in _stop_commands(settings)
