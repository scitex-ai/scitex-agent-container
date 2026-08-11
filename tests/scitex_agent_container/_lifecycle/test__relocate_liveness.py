"""Positive absence, absence of evidence, and the gap between them.

The transport's precondition is "the source is stopped". Getting it wrong in the
permissive direction produces a transcript torn mid-line, which parses, resumes,
and silently ends the conversation early — so a probe that could not answer must
refuse exactly as firmly as one that answered "yes, still running".
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_liveness import (
    interpret_liveness,
    unimplemented,
)


def _read(answers):
    return interpret_liveness(
        answers, host="h", session="tui-a", exit_code=0, stderr=""
    )


def test_a_present_session_reads_as_running() -> None:
    # Arrange: tmux is the same fact the tui runtime itself checks.
    # Act
    running, _ = _read(["yes"])
    # Assert
    assert running is True


def test_an_answering_tmux_with_no_such_session_reads_as_stopped() -> None:
    # Arrange: POSITIVE evidence of absence, from tmux's own bookkeeping — the
    # strong answer, and the only one that licenses copying.
    # Act
    running, _ = _read(["no"])
    # Assert
    assert running is False


def test_no_tmux_server_at_all_reads_as_stopped() -> None:
    # Arrange: an agent whose runtime IS a tmux session cannot be running where
    # there is no tmux server.
    # Act
    running, _ = _read(["no-server"])
    # Assert
    assert running is False


def test_tmux_not_installed_is_not_measured() -> None:
    # Arrange: the opposite of the previous case, and it looks nearly identical
    # in a shell. Nothing was measured, so nothing may be concluded.
    # Act
    running, _ = _read(["no-tmux"])
    # Assert
    assert running is None


def test_a_silent_probe_is_not_measured() -> None:
    # Arrange: no marker line means the script did not answer. Reading silence as
    # "not running" is how a live agent's transcript gets copied mid-write.
    # Act
    running, _ = _read([])
    # Assert
    assert running is None


def test_every_answer_carries_the_evidence_for_it() -> None:
    # Arrange: each caller puts this in a journal entry or a refusal, and "could
    # not determine" with no account of what was tried is not actionable.
    # Act
    _, why = _read([])
    # Assert
    assert why


def test_an_unbuilt_phase_refuses_as_unknown_rather_than_failing() -> None:
    # Arrange: nothing was attempted, so nothing about the hosts was learned.
    # Reporting it as a failure would accuse a host of something.
    effect = unimplemented("target_standby", "no adapter: the boot")
    # Act
    result = effect()
    # Assert
    assert result.ok is None


def test_an_unbuilt_phase_names_the_missing_piece() -> None:
    # Arrange: the operator's next question is always "so what IS missing", and
    # answering it in the refusal is the difference between a blocked afternoon
    # and a scoped piece of work.
    effect = unimplemented("target_standby", "no adapter: the boot")
    # Act
    result = effect()
    # Assert
    assert "no adapter: the boot" in result.detail
