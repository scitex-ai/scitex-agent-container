"""Stop-hook migration preserves ownership boundaries and operator opt-outs."""

import pytest

from scitex_agent_container.runtimes._settings_hooks import (
    _exclude_hooks,
    _strip_stale_sac_ingest_hooks,
)


@pytest.mark.parametrize(
    "command",
    [
        "scitex-agent-container dev hooks decide-stop",
        "sac dev hooks decide-stop --agent owned",
        "/usr/bin/sac dev hooks decide-stop",
        "scitex-agent-container never-stop-when-task-remains",
        "sac take-next-item",
    ],
)
def test_owned_stop_spellings_are_pruned_before_reinjection(command):
    # Arrange
    project = {"type": "command", "command": "echo preserve"}
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": command}, project]}]}

    # Act
    result = _strip_stale_sac_ingest_hooks(hooks)

    # Assert
    assert result["Stop"][0]["hooks"] == [project]


@pytest.mark.parametrize(
    "pattern",
    [
        "never-stop",
        "scitex-agent-container never-stop-when-task-remains",
        "take-next-item",
        "dev hooks decide-stop",
    ],
)
def test_historical_opt_out_patterns_apply_to_the_new_stop_command(pattern):
    # Arrange
    project = {"type": "command", "command": "echo preserve"}
    hooks = {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "scitex-agent-container dev hooks decide-stop",
                    },
                    project,
                ]
            }
        ]
    }

    # Act
    result = _exclude_hooks(hooks, [pattern])

    # Assert
    assert result["Stop"][0]["hooks"] == [project]


@pytest.mark.parametrize(
    "command",
    [
        "sac dev hooks decide-stop-custom",
        "not-sac dev hooks decide-stop",
        "nosac dev hooks decide-stop",
        "not.scitex-agent-container dev hooks decide-stop",
    ],
)
def test_similarly_named_project_command_is_not_owned_or_aliased(command):
    # Arrange
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}

    # Act
    result = _exclude_hooks(_strip_stale_sac_ingest_hooks(hooks), ["never-stop"])

    # Assert
    assert result == hooks
