"""The track command must name the verb that ACTUALLY reaches the agent.

MEASURED 2026-08-16. The non-blocking dispatch path does not deliver the
prompt; it hands the caller a command to run in a background shell, and that
command was the literal ``sac agents send``. ``send`` posts to the a2a sidecar,
which is right for an SDK-runner agent and wrong for a TUI one: the TUI
population has no recorded session id, its input is a tmux pane, and the turn
is accepted and then never processed. The caller receives a success value for a
prompt nobody will read. Seven dispatched briefs were lost that way in one
night.

So the caller was required to know whether the target runs TUI or SDK in order
to pick a verb. Operator, 2026-08-16: ``tui と sdk で不必要に変わるってキモい
んだけど``.

WHAT THESE TESTS ASSERT, and why it is the rendered command rather than the
mechanism: the VERB in the returned command must follow the resolved strategy.
That is a property of the output, so it holds however routing is implemented —
a test keyed to "does it call resolve_route" would pass a reimplementation that
called it and then ignored the answer.

PA-306: no mocks. ``track_verb_for`` and the two builders are pure functions of
a strategy string, so they are driven directly with the production constants.
"""

from __future__ import annotations

from scitex_agent_container._delivery import STRATEGY_SDK, STRATEGY_TUI
from scitex_agent_container.cli_pkg._send_track import (
    build_track_command,
    build_track_command_argv,
    track_verb_for,
)

_AGENT = "some-agent"
_PROMPT = "do the thing"


# ---------------------------------------------------------------------------
# The regression: the verb follows the route.
# ---------------------------------------------------------------------------


def test_a_tui_agent_is_reached_with_deliver():
    """The bug. `send` returns ok/200 for a TUI agent and delivers nothing."""
    # Arrange
    strategy = STRATEGY_TUI
    # Act
    verb = track_verb_for(strategy)
    # Assert
    assert verb == "deliver"


def test_an_sdk_agent_is_still_reached_with_send():
    """The non-regression. The SDK population works today and must keep working."""
    # Arrange
    strategy = STRATEGY_SDK
    # Act
    verb = track_verb_for(strategy)
    # Assert
    assert verb == "send"


def test_an_unresolvable_route_keeps_todays_verb():
    """`send` stays the default when the strategy could not be read.

    Deliberate asymmetry: only a POSITIVE identification of TUI switches the
    transport. An unresolvable route must not silently move a caller onto a
    different mechanism on a guess.
    """
    # Arrange
    strategy = None
    # Act
    verb = track_verb_for(strategy)
    # Assert
    assert verb == "send"


def test_the_tui_command_string_names_deliver():
    """The property the caller actually consumes, not the helper behind it."""
    # Arrange
    strategy = STRATEGY_TUI
    # Act
    cmd = build_track_command(_AGENT, _PROMPT, strategy=strategy)
    # Assert
    assert "sac agents deliver" in cmd


def test_the_tui_command_string_does_not_name_send():
    """Asserted from the losing side.

    `deliver` contains no substring `send`, so this cannot pass by accident —
    and a command carrying BOTH verbs would be malformed in a way the positive
    assertion alone would not catch.
    """
    # Arrange
    strategy = STRATEGY_TUI
    # Act
    cmd = build_track_command(_AGENT, _PROMPT, strategy=strategy)
    # Assert
    assert "sac agents send" not in cmd


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
    argv = build_track_command_argv(_AGENT, _PROMPT, strategy=STRATEGY_TUI)
    cmd = build_track_command(_AGENT, _PROMPT, strategy=STRATEGY_TUI)
    # Act
    verb_from_argv = argv[2]
    # Assert
    assert verb_from_argv in cmd


def test_the_argv_carries_the_prompt_unquoted():
    """argv elements are passed to exec directly — quoting them would break it."""
    # Arrange
    strategy = STRATEGY_TUI
    # Act
    argv = build_track_command_argv(_AGENT, _PROMPT, strategy=strategy)
    # Assert
    assert _PROMPT in argv


def test_a_prompt_with_shell_metacharacters_is_quoted_in_the_string():
    """The string form is pasted into a shell, so it must survive one."""
    # Arrange
    nasty = "rm -rf /; echo $HOME `whoami`"
    # Act
    cmd = build_track_command(_AGENT, nasty, strategy=STRATEGY_SDK)
    # Assert
    assert "'rm -rf /; echo $HOME `whoami`'" in cmd
