"""The never-stop-when-task-remains Stop hook must reach the settings file agents actually read.

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
_ACTUATOR = "scitex-agent-container never-stop-when-task-remains"

#: The actuator's FIRST released spelling. Already-deployed agents have this
#: string sitting in their ``settings.json`` right now, so the rename is a
#: migration, not just an edit.
_LEGACY_ACTUATOR = "scitex-agent-container take-next-item"


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
    cfg = _make_cfg("opt-out-agent", exclude_hooks=["never-stop-when-task-remains"])
    # Act
    settings = _write_and_read(tmp_path, cfg)
    # Assert
    assert _ACTUATOR not in _stop_commands(settings)


# ---------------------------------------------------------------------------
# THE RENAME MIGRATION: every already-deployed agent goes through this
# ---------------------------------------------------------------------------


def _seed_legacy_settings(tmp_path: Path) -> None:
    """Write a settings.json exactly as an already-deployed agent has it —
    carrying the actuator's FIRST released name."""
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
                                {
                                    "type": "command",
                                    "command": "scitex-agent-container event ingest stop",
                                },
                                {"type": "command", "command": _LEGACY_ACTUATOR},
                            ],
                        }
                    ]
                }
            }
        )
    )


def test_legacy_actuator_name_is_removed_on_rematerialise(tmp_path: Path):
    """The old name must be PRUNED, not left beside the new one.

    De-dupe compares whole matcher-groups, so to that logic a renamed
    command is a NEW command — without an explicit prune the old hook
    survives. And it no longer resolves to a registered CLI command, so it
    would fail loudly on every single turn end.
    """
    # Arrange
    _seed_legacy_settings(tmp_path)
    # Act
    settings = _write_and_read(tmp_path)
    # Assert
    assert _LEGACY_ACTUATOR not in _stop_commands(settings)


def test_new_actuator_name_is_present_after_migration(tmp_path: Path):
    # Arrange
    _seed_legacy_settings(tmp_path)
    # Act
    settings = _write_and_read(tmp_path)
    # Assert
    assert _ACTUATOR in _stop_commands(settings)


def test_migration_leaves_exactly_one_actuator(tmp_path: Path):
    """Not both. This is the whole point of the transition test."""
    # Arrange
    _seed_legacy_settings(tmp_path)
    # Act
    commands = _stop_commands(_write_and_read(tmp_path))
    actuators = [
        c
        for c in commands
        if "never-stop-when-task-remains" in c or "take-next-item" in c
    ]
    # Assert
    assert actuators == [_ACTUATOR]


def test_migration_preserves_a_non_sac_baseline_hook(tmp_path: Path):
    """Pruning SAC-owned hooks must not take a project's own gate with it."""
    # Arrange
    _seed_legacy_settings(tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    data["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "clew verify --strict"}
    )
    path.write_text(json.dumps(data))
    # Act
    settings = _write_and_read(tmp_path)
    # Assert
    assert "clew verify --strict" in _stop_commands(settings)


def test_migration_is_idempotent(tmp_path: Path):
    """Running it twice must not resurrect or duplicate anything."""
    # Arrange
    _seed_legacy_settings(tmp_path)
    _write_and_read(tmp_path)
    # Act
    commands = _stop_commands(_write_and_read(tmp_path))
    # Assert
    assert commands.count(_ACTUATOR) == 1
