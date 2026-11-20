"""ADR-0033: the Hermes adapter (neutral layer -> profile hooks mapping)."""

from __future__ import annotations

from scitex_agent_container._neutral_config import load_layer
from scitex_agent_container._neutral_config._hermes import (
    hermes_event,
    render_command,
    render_hooks,
)
from scitex_agent_container._neutral_config._model import Hook

from ._layer_fixture import make_layer


def test_pre_tool_renders_as_hermes_pre_tool_call(user_layer):
    # Arrange
    layer = user_layer
    # Act
    handlers = render_hooks(layer.hooks)["pre_tool_call"]
    # Assert
    assert handlers == [{"command": "hooks/guard.sh", "matcher": "Bash"}]


def test_bash_matcher_passes_through_verbatim(user_layer):
    # Arrange
    layer = user_layer
    # Act
    matcher = render_hooks(layer.hooks)["pre_tool_call"][0]["matcher"]
    # Assert
    assert matcher == "Bash"


def test_hook_for_another_harness_is_skipped(user_layer):
    # Arrange
    layer = user_layer
    # Act
    rendered = render_hooks(layer.hooks)
    # Assert
    assert "Weird" not in rendered


def test_neutral_stop_has_no_measured_hermes_counterpart():
    # Arrange
    hook = Hook(name="s", event="stop", command="hooks/s.sh")
    # Act
    event = hermes_event(hook)
    # Assert
    assert event is None


def test_hermes_scoped_native_event_renders(tmp_path):
    # Arrange
    layer = load_layer(
        make_layer(
            tmp_path / "h",
            hooks=[
                {
                    "name": "lint",
                    "event": "hermes:post_tool_call",
                    "command": "/abs/lint.sh",
                    "matcher": "^(?:Bash|Write)$",
                    "timeout": 30,
                }
            ],
        )
    )
    # Act
    handlers = render_hooks(layer.hooks)["post_tool_call"]
    # Assert
    assert handlers == [
        {"command": "/abs/lint.sh", "matcher": "^(?:Bash|Write)$", "timeout": 30}
    ]


def test_absolute_command_passes_through_verbatim():
    # Arrange
    command = "/scratch/x/bin/python -m scitex_dev.hooks.dispatch"
    # Act
    rendered = render_command(command)
    # Assert
    assert rendered == command


def test_empty_matcher_omits_the_key(tmp_path):
    # Arrange
    layer = load_layer(
        make_layer(
            tmp_path / "e",
            hooks=[{"name": "n", "event": "post_tool", "command": "hooks/n.sh"}],
        )
    )
    # Act
    handler = render_hooks(layer.hooks)["post_tool_call"][0]
    # Assert
    assert "matcher" not in handler


def test_timeout_is_preserved(tmp_path):
    # Arrange
    layer = load_layer(
        make_layer(
            tmp_path / "t",
            hooks=[
                {
                    "name": "n",
                    "event": "post_tool",
                    "command": "hooks/n.sh",
                    "timeout": 30,
                }
            ],
        )
    )
    # Act
    handler = render_hooks(layer.hooks)["post_tool_call"][0]
    # Assert
    assert handler["timeout"] == 30
