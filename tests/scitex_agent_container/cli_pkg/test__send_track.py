"""Tracking commands use only the native HTTP turn protocol."""

from __future__ import annotations

from scitex_agent_container.cli_pkg._send_track import (
    build_track_command,
    build_track_command_argv,
)

_AGENT = "some-agent"
_PROMPT = "do the thing"


# ---------------------------------------------------------------------------
# The regression: a TUI hint cannot select keyboard injection.
# ---------------------------------------------------------------------------


def test_track_command_names_native_http_send():
    # Arrange
    name = _AGENT
    # Act
    command = build_track_command(name, _PROMPT)
    # Assert
    assert "sac agents send" in command


def test_track_command_never_names_terminal_deliver():
    # Arrange
    name = _AGENT
    # Act
    command = build_track_command(name, _PROMPT)
    # Assert
    assert "sac agents deliver" not in command


# ---------------------------------------------------------------------------
# The anti-drift property: two renderings, one decision.
# ---------------------------------------------------------------------------


def test_the_argv_and_the_string_agree_on_the_verb():
    """They used to be built from two independent literals.

    The dispatch payload carries both `track_command` (shell string) and
    `track_command_argv` (list). Changing one and forgetting the other would
    hand the caller a working string and a broken argv, or the reverse.
    """
    # Arrange
    argv = build_track_command_argv(_AGENT, _PROMPT)
    cmd = build_track_command(_AGENT, _PROMPT)
    # Act
    verb_from_argv = argv[2]
    # Assert
    assert verb_from_argv in cmd


def test_the_argv_carries_the_prompt_unquoted():
    """argv elements are passed to exec directly — quoting them would break it."""
    # Arrange
    # Act
    argv = build_track_command_argv(_AGENT, _PROMPT)
    # Assert
    assert _PROMPT in argv


def test_a_prompt_with_shell_metacharacters_is_quoted_in_the_string():
    """The string form is pasted into a shell, so it must survive one."""
    # Arrange
    nasty = "rm -rf /; echo $HOME `whoami`"
    # Act
    cmd = build_track_command(_AGENT, nasty)
    # Assert
    assert "'rm -rf /; echo $HOME `whoami`'" in cmd
