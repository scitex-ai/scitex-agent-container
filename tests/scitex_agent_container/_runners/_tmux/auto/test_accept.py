"""PS-202 mirror: real tests for the ``auto.accept`` pure-action layer.

Covers the two facets the action layer must get right:

* ``_yn_has_yes_option`` — the safety check that gates blind-pressing
  ``1`` on a y/n prompt. Must recognize the documented option formats
  and refuse when none is present.
* ``respond`` — the dispatcher. Verified via injected ``send_fn`` /
  ``dm_fn`` so the tests never touch ``tmux`` / ``curl``.

Test style (STX-TQ002 / TQ007): explicit ``# Arrange`` / ``# Act`` /
``# Assert`` markers in order; one assertion per test.
"""

from __future__ import annotations

from scitex_agent_container._runners._tmux.auto.accept import (
    _yn_has_yes_option,
    respond,
)

# ---------------------------------------------------------------------------
# _yn_has_yes_option — pane-text recognizer
# ---------------------------------------------------------------------------


def test_yn_has_yes_option_recognizes_bracket_yes_marker():
    # Arrange
    pane = "Continue? [1] Yes  [2] No"
    # Act
    detected = _yn_has_yes_option(pane)
    # Assert
    assert detected is True


def test_yn_has_yes_option_returns_false_when_marker_absent():
    # Arrange
    pane = "Working… esc to interrupt"
    # Act
    detected = _yn_has_yes_option(pane)
    # Assert
    assert detected is False


# ---------------------------------------------------------------------------
# respond — state → action dispatch
# ---------------------------------------------------------------------------


def _record_send():
    """Return (recorder, calls_list); recorder mimics _tmux_send signature."""
    calls: list[tuple] = []

    def send(*keys: str) -> None:
        calls.append(keys)

    return send, calls


def _record_dm():
    """Return (recorder, calls_list); recorder mimics _orochi_dm signature."""
    calls: list[tuple[str, str]] = []

    def dm(channel: str, message: str) -> None:
        calls.append((channel, message))

    return dm, calls


def test_respond_compose_pending_sends_enter_through_injected_fn():
    # Arrange
    send, send_calls = _record_send()
    dm, _dm_calls = _record_dm()
    # Act
    respond("alpha", "compose_pending_unsent", send_fn=send, dm_fn=dm)
    # Assert
    assert send_calls == [("Enter",)]


def test_respond_y_n_prompt_skips_blind_send_when_yes_absent():
    # Arrange
    send, send_calls = _record_send()
    dm, _dm_calls = _record_dm()
    # Act
    respond(
        "beta",
        "y_n_prompt",
        pane_text="Working… esc to interrupt",
        send_fn=send,
        dm_fn=dm,
    )
    # Assert
    assert send_calls == []


def test_respond_auth_error_escalates_via_dm_to_mgr_auth():
    # Arrange
    send, _send_calls = _record_send()
    dm, dm_calls = _record_dm()
    # Act
    respond("gamma", "auth_error", send_fn=send, dm_fn=dm)
    # Assert
    assert dm_calls[0][0] == "mgr-auth"
