"""Regression: the submit verifier could not see a Codex composer.

``_compose_pending_live`` recognises only Claude Code's "❯" box. A Codex
pane draws "›", so the predicate answered False no matter what the
composer held, phase 1 of :func:`verify_submit_by_advancement` concluded
"nothing to submit", and the function returned True over a payload that
was sitting there unsent.

Measured on handyman-01 (2026-09-05 11:31 UTC): ``sac agents deliver``
reported "DELIVERED and SUBMITTED" and exited 0 while the message stayed
in the composer with no in-progress marker; one Enter sent by hand then
started the turn. A false GREEN, which is worse than a failure: fleet
automation believes the agent was told something it never received.

The fix lets a caller name what it pasted. Claude's "❯" signal stays
primary and unchanged (its large pastes collapse to "[Pasted text #1 …]",
so a fragment alone would be invisible there); the fragment is consulted
IN ADDITION, for a composer the marker test cannot see.

Real fake callables, no mocks (PA-306). One assertion per test (STX-TQ007).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scitex_agent_container.runtimes._tui_compose import (
    composer_holds_fragment,
    fragment_tail,
    verify_submit_by_advancement,
)

# ── Real Codex pane snapshots (shape taken from tui-handyman-01) ────────────

_CODEX_PENDING = (
    "  Tip: New Build faster with Codex.\n"
    "› You are a handyman. Say plainly what you are uncertain about.\n"
    "  Start or continue.[sac-deliver:8bc1778892eb] CHANNEL CHECK: answer\n"
    "  with the single word ARRIVED and stop.\n"
    "  qwen38-27b default · /home/ywatanabe/proj/local-coder\n"
)

# After the Enter lands: the message moved into the TRANSCRIPT (which keeps
# its own "›"), and the composer below shows a rotating placeholder hint.
_CODEX_SUBMITTED = (
    "› Start or continue.[sac-deliver:8bc1778892eb] CHANNEL CHECK: answer\n"
    "  with the single word ARRIVED and stop.\n"
    "• Working (12s • esc to interrupt)\n"
    "› Write tests for @filename\n"
    "  qwen38-27b default · /home/ywatanabe/proj/local-coder\n"
)

_TOKEN = "[sac-deliver:8bc1778892eb]"


@dataclass
class _RecordingSend:
    keys: list[str] = field(default_factory=list)

    def __call__(self, key: str) -> None:
        self.keys.append(key)


@dataclass
class _PaneAfterEnter:
    """Serves the pending pane until an Enter arrives, then the submitted one."""

    send: _RecordingSend

    def __call__(self, _name: str) -> str:
        return _CODEX_SUBMITTED if self.send.keys else _CODEX_PENDING


def test_a_codex_composer_holding_the_payload_reads_as_pending():
    # Arrange
    pane = _CODEX_PENDING
    # Act
    held = composer_holds_fragment(pane, _TOKEN)
    # Assert
    assert held is True


def test_the_same_payload_above_the_composer_reads_as_submitted():
    # Arrange -- the transcript copy carries its own "›" marker.
    pane = _CODEX_SUBMITTED
    # Act
    held = composer_holds_fragment(pane, _TOKEN)
    # Assert
    assert held is False


def test_an_empty_fragment_never_reports_pending():
    # Arrange
    pane = _CODEX_PENDING
    # Act
    held = composer_holds_fragment(pane, "   ")
    # Assert
    assert held is False


def test_the_tail_is_taken_from_the_end_of_the_payload():
    # Arrange
    payload = "x" * 200 + "THE END"
    # Act
    tail = fragment_tail(payload, limit=7)
    # Assert
    assert tail == "THE END"


def test_the_verifier_sends_enter_into_a_codex_composer():
    # Arrange
    send = _RecordingSend()
    capture = _PaneAfterEnter(send)
    # Act
    verify_submit_by_advancement(
        "handyman-01",
        capture_fn=capture,
        send_keys_fn=send,
        pending_fragment=_TOKEN,
        poll_s=0.0,
        appear_timeout_s=1.0,
        idle_wait_s=1.0,
        sleep_fn=lambda _s: None,
        time_fn=_ticking(),
    )
    # Assert
    assert send.keys == ["Enter"]


def test_the_verifier_reports_success_once_the_codex_pane_advances():
    # Arrange
    send = _RecordingSend()
    capture = _PaneAfterEnter(send)
    # Act
    submitted = verify_submit_by_advancement(
        "handyman-01",
        capture_fn=capture,
        send_keys_fn=send,
        pending_fragment=_TOKEN,
        poll_s=0.0,
        appear_timeout_s=1.0,
        idle_wait_s=1.0,
        sleep_fn=lambda _s: None,
        time_fn=_ticking(),
    )
    # Assert
    assert submitted is True


def test_without_a_fragment_a_codex_pane_behaves_as_before():
    # Arrange -- no fragment: only Claude's "❯" test applies, which a Codex
    # pane never satisfies, so the verifier still finds nothing to submit.
    send = _RecordingSend()
    # Act
    submitted = verify_submit_by_advancement(
        "handyman-01",
        capture_fn=lambda _name: _CODEX_PENDING,
        send_keys_fn=send,
        poll_s=0.0,
        appear_timeout_s=1.0,
        idle_wait_s=1.0,
        sleep_fn=lambda _s: None,
        time_fn=_ticking(),
    )
    # Assert
    assert submitted is True


def _ticking():
    """A monotonic clock that advances 0.1s per read (no real sleeping)."""
    state = {"now": 0.0}

    def _now() -> float:
        state["now"] += 0.1
        return state["now"]

    return _now
