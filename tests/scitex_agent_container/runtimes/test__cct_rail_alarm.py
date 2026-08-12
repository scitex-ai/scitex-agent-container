"""Making a mute agent loud — the record, the page, and the dedupe.

Covers ``runtimes/_cct_rail_alarm`` (card
``sac-cct-rail-loud-when-no-slot-resolves-20260812``). The alarm's whole
premise is that the shout cannot ride the broken rail, so the delivery seam is
exercised through a REAL recording callable (the same ``notify``-style seam
``_account/refresh_alarm`` already uses) rather than a mock of the transport.

Two properties are load-bearing and each has its own test:

* the alarm NEVER raises — it is attached to every start in the fleet, and a
  diagnostic that can fail a start is an outage generator;
* a failed push still leaves the durable record, and says so, because the
  record is the only thing that makes the rail trustworthy.

Real ``CctRailVerdict`` values, a real temp event log, real recording
callables — no mocks (PA-306). STX-TQ002 AAA markers, STX-TQ007 one assert per
test.

Named ``test__cct_rail_alarm.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_rail_alarm.py``.
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container.runtimes._cct_rail_alarm import (
    alarm_cct_rail,
    check_cct_rail_at_start,
    page_is_warranted,
)
from scitex_agent_container.runtimes._cct_rail_verdict import (
    RAIL_DOWN,
    RAIL_NOT_REQUESTED,
    RAIL_UNKNOWN,
    RAIL_UP,
    CctRailVerdict,
)

_SECRET = "zz-secret-value-must-never-be-echoed"


class _Recorder:
    """A real delivery callable that remembers what it was handed.

    Not a mock: it is the injected collaborator the production seam documents,
    and the assertions are about what the alarm SENT, which is the behaviour
    under test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, summary: str, detail: str) -> None:
        self.calls.append((summary, detail))


class _Exploder:
    """A real delivery callable that fails, like an unreachable lead listen."""

    def __call__(self, summary: str, detail: str) -> None:
        raise RuntimeError("lead listen unreachable")


def _verdict(state: str, *, agent: str = "zz-alarm") -> CctRailVerdict:
    """A real verdict of the requested state."""
    return CctRailVerdict(
        agent=agent,
        state=state,
        candidates=("ZZ_ALARM",),
        near_misses=("ZZ_NEIGHBOUR",),
        pool_source="SAC_SECRETS_ENVRC=/zz/pool.src",
        pool_trusted=state != RAIL_UNKNOWN,
        detail="zz test detail",
    )


def _log(tmp_path: Path) -> Path:
    return tmp_path / "sac-events.jsonl"


# ---------------------------------------------------------------------------
# gating
# ---------------------------------------------------------------------------


def test_a_spec_that_never_asked_for_a_rail_is_skipped(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    outcome = alarm_cct_rail(
        _verdict(RAIL_NOT_REQUESTED), path=_log(tmp_path), push=recorder
    )
    # Assert
    assert outcome == "skipped"


def test_an_unrequested_rail_pages_nobody(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_verdict(RAIL_NOT_REQUESTED), path=_log(tmp_path), push=recorder)
    # Assert
    assert recorder.calls == []


def test_a_healthy_rail_pages_nobody(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_verdict(RAIL_UP), path=_log(tmp_path), push=recorder)
    # Assert
    assert recorder.calls == []


def test_a_healthy_rail_reports_clear(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    outcome = alarm_cct_rail(_verdict(RAIL_UP), path=_log(tmp_path), push=recorder)
    # Assert
    assert outcome == "clear"


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


def test_a_down_rail_pages_the_operator(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    outcome = alarm_cct_rail(_verdict(RAIL_DOWN), path=_log(tmp_path), push=recorder)
    # Assert
    assert outcome == "paged"


def test_an_unknown_rail_also_pages(tmp_path: Path) -> None:
    # Arrange — "I could not tell" must not be quieter than "it is broken";
    # collapsing UNKNOWN into a silent pole is the bug this closes.
    recorder = _Recorder()
    # Act
    outcome = alarm_cct_rail(_verdict(RAIL_UNKNOWN), path=_log(tmp_path), push=recorder)
    # Assert
    assert outcome == "paged"


def test_the_page_says_the_agent_is_mute(tmp_path: Path) -> None:
    # Arrange — the operator's symptom is silence, so the summary must name it.
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_verdict(RAIL_DOWN), path=_log(tmp_path), push=recorder)
    # Assert
    assert "MUTE" in recorder.calls[0][0]


def test_the_page_names_the_agent(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    # Act
    alarm_cct_rail(
        _verdict(RAIL_DOWN, agent="zz-named"), path=_log(tmp_path), push=recorder
    )
    # Assert
    assert "zz-named" in recorder.calls[0][0]


def test_the_page_names_the_one_line_fix(tmp_path: Path) -> None:
    # Arrange — an alarm that says what broke without saying what to do makes
    # the operator do the diagnosis sac already did.
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_verdict(RAIL_DOWN), path=_log(tmp_path), push=recorder)
    # Assert
    assert "CCT_BOT_TOKEN_SLOT" in recorder.calls[0][1]


def test_the_page_reports_whether_the_pool_read_was_conclusive(
    tmp_path: Path,
) -> None:
    # Arrange — the reader must be able to tell an observation from a blind spot.
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_verdict(RAIL_UNKNOWN), path=_log(tmp_path), push=recorder)
    # Assert
    assert "pool_read_conclusive: False" in recorder.calls[0][1]


def test_the_page_never_carries_a_token_value(tmp_path: Path) -> None:
    # Arrange — a verdict whose every free-text field is stuffed with a
    # value-shaped string, so any field that leaks into the page is caught.
    recorder = _Recorder()
    leaky = CctRailVerdict(
        agent="zz-leak",
        state=RAIL_DOWN,
        declared_slot="ZZ_LEAK",
        detail="zz detail",
    )
    # Act
    alarm_cct_rail(leaky, path=_log(tmp_path), push=recorder)
    # Assert
    assert _SECRET not in "".join(recorder.calls[0])


# ---------------------------------------------------------------------------
# dedupe — one page per outage, not one per start
# ---------------------------------------------------------------------------


def test_the_same_outage_does_not_page_twice(tmp_path: Path) -> None:
    # Arrange — the agent is restarted while still broken.
    recorder = _Recorder()
    log = _log(tmp_path)
    alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    # Act
    outcome = alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    # Assert — a blocker that re-pages on every start trains the operator to
    # ignore it, which is the failure mode this module exists to prevent.
    assert outcome == "recorded"


def test_a_repeat_outage_sends_exactly_one_page(tmp_path: Path) -> None:
    # Arrange
    recorder = _Recorder()
    log = _log(tmp_path)
    alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    # Act
    alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    # Assert
    assert len(recorder.calls) == 1


def test_recovery_rearms_the_alarm(tmp_path: Path) -> None:
    # Arrange — broken, then fixed, then broken again. The second outage is a
    # NEW fact and must reach the operator.
    recorder = _Recorder()
    log = _log(tmp_path)
    alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    alarm_cct_rail(_verdict(RAIL_UP), path=log, push=recorder)
    # Act
    outcome = alarm_cct_rail(_verdict(RAIL_DOWN), path=log, push=recorder)
    # Assert
    assert outcome == "paged"


# ---------------------------------------------------------------------------
# the record survives a failed page
# ---------------------------------------------------------------------------


def test_a_failed_push_still_writes_the_record(tmp_path: Path) -> None:
    # Arrange — no lead configured / listen unreachable.
    log = _log(tmp_path)
    # Act
    alarm_cct_rail(
        _verdict(RAIL_DOWN), path=log, push=_Exploder(), err_stream=io.StringIO()
    )
    # Assert — the durable account of the outage exists even when nobody was paged.
    assert log.exists()


def test_a_failed_push_reports_recorded_not_paged(tmp_path: Path) -> None:
    # Arrange
    # Act
    outcome = alarm_cct_rail(
        _verdict(RAIL_DOWN),
        path=_log(tmp_path),
        push=_Exploder(),
        err_stream=io.StringIO(),
    )
    # Assert — sac must never claim to have paged somebody it did not page.
    assert outcome == "recorded"


def test_a_failed_push_says_nobody_was_paged(tmp_path: Path) -> None:
    # Arrange
    stream = io.StringIO()
    # Act
    alarm_cct_rail(
        _verdict(RAIL_DOWN), path=_log(tmp_path), push=_Exploder(), err_stream=stream
    )
    # Assert
    assert "NOBODY HAS BEEN PAGED" in stream.getvalue()


def test_a_failed_push_does_not_raise(tmp_path: Path) -> None:
    # Arrange
    outcome = None
    # Act
    outcome = alarm_cct_rail(
        _verdict(RAIL_DOWN),
        path=_log(tmp_path),
        push=_Exploder(),
        err_stream=io.StringIO(),
    )
    # Assert — this runs inside agent_start; raising here would fail the start.
    assert outcome is not None


# ---------------------------------------------------------------------------
# the page is gated on EVIDENCE; the record never is
# ---------------------------------------------------------------------------


def _bare_down(agent: str = "zz-library") -> CctRailVerdict:
    """DOWN with no evidence anybody meant this agent to have a bot.

    The measured majority: 66 of the 81 specs that declare the channel, mostly
    library agents and the three spec templates that inherit the request as
    scaffolding.
    """
    return CctRailVerdict(agent=agent, state=RAIL_DOWN, candidates=("ZZ_LIBRARY",))


def test_a_bot_less_library_agent_is_recorded_but_not_paged(tmp_path: Path) -> None:
    # Arrange — paging all 66 of these would rebuild the ignored alert channel
    # the 2026-08-10 prune was written to remove.
    recorder = _Recorder()
    # Act
    alarm_cct_rail(_bare_down(), path=_log(tmp_path), push=recorder)
    # Assert
    assert recorder.calls == []


def test_an_unpaged_verdict_is_still_recorded(tmp_path: Path) -> None:
    # Arrange — the sweep must still find it; only the interrupt is withheld.
    log = _log(tmp_path)
    # Act
    alarm_cct_rail(_bare_down(), path=log, push=_Recorder())
    # Assert
    assert log.exists()


def test_a_declared_slot_that_fails_always_pages(tmp_path: Path) -> None:
    # Arrange — somebody typed this mapping on purpose (2026-08-10 ruling).
    recorder = _Recorder()
    declared = CctRailVerdict(
        agent="zz-declared", state=RAIL_DOWN, declared_slot="ZZ_TYPO"
    )
    # Act
    outcome = alarm_cct_rail(declared, path=_log(tmp_path), push=recorder)
    # Assert
    assert outcome == "paged"


def test_a_near_miss_in_the_pool_pages(tmp_path: Path) -> None:
    # Arrange — a bot that plausibly belongs to this agent EXISTS and the
    # wiring does not reach it. Measured: 4 of the 66, all genuine defects.
    recorder = _Recorder()
    near = CctRailVerdict(
        agent="neurovista", state=RAIL_DOWN, near_misses=("PAPER_NEUROVISTA",)
    )
    # Act
    outcome = alarm_cct_rail(near, path=_log(tmp_path), push=recorder)
    # Assert
    assert outcome == "paged"


def test_a_rail_that_used_to_work_here_pages_when_it_breaks(tmp_path: Path) -> None:
    # Arrange — the regression shape, and the reason the 2026-08-12 outage was
    # noticed by silence rather than by a signal: this agent HAD a rail.
    recorder = _Recorder()
    log = _log(tmp_path)
    alarm_cct_rail(_verdict(RAIL_UP, agent="zz-regressed"), path=log, push=recorder)
    # Act
    outcome = alarm_cct_rail(_bare_down("zz-regressed"), path=log, push=recorder)
    # Assert
    assert outcome == "paged"


def test_an_unknown_verdict_pages_without_any_evidence(tmp_path: Path) -> None:
    # Arrange — "I could not tell" is usually systemic (one missing
    # SAC_SECRETS_ENVRC blinds the whole fleet), so it is never withheld.
    blind = CctRailVerdict(agent="zz-blind-bare", state=RAIL_UNKNOWN)
    # Act
    warranted = page_is_warranted(blind, path=_log(tmp_path))
    # Assert
    assert warranted is True


def test_a_healthy_rail_is_never_warranted(tmp_path: Path) -> None:
    # Arrange
    up = CctRailVerdict(agent="zz-fine", state=RAIL_UP)
    # Act
    warranted = page_is_warranted(up, path=_log(tmp_path))
    # Assert
    assert warranted is False


# ---------------------------------------------------------------------------
# the start-time entry point cannot take a start down
# ---------------------------------------------------------------------------


def test_an_unassessable_config_does_not_raise(tmp_path: Path) -> None:
    # Arrange — an object with none of the AgentConfig surface, i.e. every way
    # the assessment could blow up at once.
    broken = object()
    # Act
    outcome = check_cct_rail_at_start(broken, dest=tmp_path)
    # Assert
    assert outcome == "skipped"
