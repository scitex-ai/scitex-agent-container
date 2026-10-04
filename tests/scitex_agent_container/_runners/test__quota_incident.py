#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Route-specific quota incident + admission (the quota-stop slice).

Card ``sac-codex-python-sdk-harness-20260814``. A structured
``usageLimitExceeded`` must LATCH: no fresh same-route backend turn while
exhausted, a BLOCKED beat the daemon preserves from durable state, held
(never dropped, never completed) envelopes, once-only accepted
notification and evidence-ordered recovery — all offline-testable here
with hand-rolled stand-ins (the ``_Scripted*`` idiom), never mocks. AAA +
one assert per test.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from scitex_agent_container._runners._codex_turn_driver import _drain_codex_inbox
from scitex_agent_container._runners._daemon_contract import make_daemon_state_fn
from scitex_agent_container._runners._harness_session import NormalizedEvent, RunResult
from scitex_agent_container._runners._harness_turn_pump import drive_harness_turn
from scitex_agent_container._runners._incarnation import WRITER_TURN_DRIVER
from scitex_agent_container._runners._quota_incident import (
    QuotaBlockedError,
    classify_quota_exceeded,
    clear_quota_incident,
    notify_quota_incident_once,
    quota_admits,
    record_quota_incident,
)
from scitex_agent_container._runners._session_beat import (
    STATE_BLOCKED,
    STATE_BUSY,
    STATE_READY,
    read_heartbeat,
    write_heartbeat,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)

_ROUTE = "codex-sdk"
_TIME = 100.0
_TIMEOUT_S = 15


def _limited(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=_TIMEOUT_S))


def _cap_error_event(detail="quota wall"):
    return NormalizedEvent(
        kind="error",
        error=detail,
        raw={"codex_error_info": "usageLimitExceeded"},
    )


def _good_evidence(at=_TIME + 1.0):
    return {"at": at, "kind": "probe-success"}


def test_usage_limit_exceeded_on_codex_is_quota():
    # Arrange
    # Act
    got = classify_quota_exceeded(harness="codex-sdk", codex_error_info="usageLimitExceeded")
    # Assert
    assert got is True


def test_rate_limit_exceeded_is_not_quota():
    # Arrange
    # Act
    got = classify_quota_exceeded(harness="codex-sdk", codex_error_info="rateLimitExceeded")
    # Assert
    assert got is False


def test_context_window_exceeded_is_not_quota():
    # Arrange
    # Act
    got = classify_quota_exceeded(harness="codex-sdk", codex_error_info="contextWindowExceeded")
    # Assert
    assert got is False


def test_session_budget_exceeded_is_not_quota():
    # Arrange
    # Act
    got = classify_quota_exceeded(harness="codex-sdk", codex_error_info="sessionBudgetExceeded")
    # Assert
    assert got is False


def test_quoted_weekly_limit_banner_is_not_quota():
    # Arrange — prose banner, no structured signal (repo refuses banner matching).
    # Act
    got = classify_quota_exceeded(harness="codex-sdk", codex_error_info=None)
    # Assert
    assert got is False


def test_other_harness_usage_cap_is_not_this_route_quota():
    # Arrange
    # Act
    got = classify_quota_exceeded(harness="openai", codex_error_info="usageLimitExceeded")
    # Assert
    assert got is False


def test_recorded_incident_refuses_admission(tmp_path):
    # Arrange
    # Act
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Assert
    assert quota_admits(tmp_path, _ROUTE) is False


def test_corrupt_incident_file_denies(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text("{partial", encoding="utf-8")
    # Act
    got = quota_admits(tmp_path, _ROUTE)
    # Assert
    assert got is False


def test_other_route_record_survives_this_route_write(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route="other", detail="x", at=_TIME)
    # Act
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Assert
    assert quota_admits(tmp_path, "other") is False


def test_stale_evidence_cannot_clear(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Act
    cleared = clear_quota_incident(tmp_path, route=_ROUTE, evidence={"at": 99.0, "kind": "probe-success"})
    # Assert
    assert cleared is False and quota_admits(tmp_path, _ROUTE) is False


def test_nan_evidence_cannot_clear(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Act
    cleared = clear_quota_incident(tmp_path, route=_ROUTE, evidence={"at": float("nan"), "kind": "probe-success"})
    # Assert
    assert cleared is False and quota_admits(tmp_path, _ROUTE) is False


def test_unrecognized_kind_cannot_clear(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Act
    cleared = clear_quota_incident(tmp_path, route=_ROUTE, evidence={"at": 101.0, "kind": "vibes"})
    # Assert
    assert cleared is False and quota_admits(tmp_path, _ROUTE) is False


def test_verified_evidence_clears(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Act
    cleared = clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
    # Assert
    assert cleared is True and quota_admits(tmp_path, _ROUTE) is True


def test_second_cap_after_clear_alerts_again(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="one", at=_TIME)
    clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
    seen = []
    # Act
    record_quota_incident(tmp_path, route=_ROUTE, detail="two", at=102.0)
    notified = notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: seen.append(r) or 7)
    # Assert
    assert notified is True and len(seen) == 1


def test_unaccepted_notification_stays_pending(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    # Act
    first = notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: None)
    second = notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: 9)
    # Assert
    assert (first, second) == (False, True)


def test_accepted_notification_fires_exactly_once(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    seen = []
    # Act
    first = notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: seen.append(r) or 7)
    second = notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: seen.append(r) or 8)
    # Assert
    assert (first, second, len(seen)) == (True, False, 1)


def test_notification_delivers_live_incident_record(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    seen = []
    # Act
    notify_quota_incident_once(tmp_path, route=_ROUTE, notify=lambda r: seen.append(r) or 7)
    # Assert
    assert (seen[0]["route"], seen[0]["cause"]) == (_ROUTE, "quota-exhausted")


def test_later_turns_extend_one_incident(tmp_path):
    # Arrange
    # Act
    record_quota_incident(tmp_path, route=_ROUTE, detail="one", at=100.0)
    record = record_quota_incident(tmp_path, route=_ROUTE, detail="two", at=101.0)
    # Assert
    assert [e["detail"] for e in record["evidence"]] == ["one", "two"]


class _ErrorSession:
    """Yields one error event, then a terminal result (unreached)."""

    def __init__(self, event):
        self.event = event
        self.sends = 0

    async def send(self, message):
        self.sends += 1
        yield self.event


def _pump_error_turn(tmp_path, event, harness="codex-sdk", session=None):
    async def _scenario():
        loop = asyncio.get_running_loop()
        env = SimpleNamespace(text="hi", response=loop.create_future(), session_id=None)
        stop = asyncio.Event()
        await drive_harness_turn(
            session if session is not None else _ErrorSession(event),
            env,
            state_dir=tmp_path,
            pid=1,
            stop=stop,
            print_stream=False,
            name="agent",
            host=None,
            harness=harness,
        )
        return env

    return _limited(_scenario())


def test_quota_error_turn_blocks_the_route(tmp_path):
    # Arrange
    # Act
    _pump_error_turn(tmp_path, _cap_error_event())
    # Assert
    assert quota_admits(tmp_path, _ROUTE) is False


def test_quota_error_beat_is_blocked_not_ready(tmp_path):
    # Arrange
    # Act
    _pump_error_turn(tmp_path, _cap_error_event())
    # Assert
    assert read_heartbeat(tmp_path)["state"] == STATE_BLOCKED


def test_direct_pump_call_on_latched_route_burns_nothing(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    session = _ErrorSession(_cap_error_event())
    # Act: the SAME counted session drives the gated pump call, so the
    # send count proves no backend turn was created.
    env = _pump_error_turn(tmp_path, _cap_error_event(), session=session)
    # Assert
    assert session.sends == 0


def test_quota_error_turn_fails_envelope_with_blocked_error(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    session = _ErrorSession(_cap_error_event())
    # Act
    env = _pump_error_turn(tmp_path, _cap_error_event(), session=session)
    # Assert
    assert isinstance(env.response.exception(), QuotaBlockedError)


def test_generic_error_turn_beat_stays_ready_without_incident(tmp_path):
    # Arrange
    generic = NormalizedEvent(kind="error", error="boom", raw={})
    # Act
    _pump_error_turn(tmp_path, generic)
    # Assert
    assert read_heartbeat(tmp_path)["state"] == STATE_READY


def test_generic_error_turn_admits_without_incident(tmp_path):
    # Arrange
    generic = NormalizedEvent(kind="error", error="boom", raw={})
    # Act
    _pump_error_turn(tmp_path, generic)
    # Assert
    assert quota_admits(tmp_path, _ROUTE) is True


def test_rate_limit_turn_beat_stays_ready(tmp_path):
    # Arrange
    limited = NormalizedEvent(
        kind="error",
        error="slow down",
        raw={"codex_error_info": "rateLimitExceeded"},
    )
    # Act
    _pump_error_turn(tmp_path, limited)
    # Assert
    assert read_heartbeat(tmp_path)["state"] == STATE_READY


def test_rate_limit_turn_writes_no_incident(tmp_path):
    # Arrange
    limited = NormalizedEvent(
        kind="error",
        error="slow down",
        raw={"codex_error_info": "rateLimitExceeded"},
    )
    # Act
    _pump_error_turn(tmp_path, limited)
    # Assert
    assert (tmp_path / "quota_incident.json").exists() is False


def test_weekly_limit_banner_turn_is_conserved_not_latched(tmp_path):
    # Arrange
    banner = NormalizedEvent(
        kind="error",
        error="You've hit your weekly limit · resets Monday",
        raw={"codex_error_info": None},
    )
    # Act
    _pump_error_turn(tmp_path, banner)
    # Assert
    assert quota_admits(tmp_path, _ROUTE) is True


class _ScriptedBackend:
    """Counts backend turns; answers every turn with a canned result."""

    def __init__(self):
        self.texts = []

    async def send(self, message):
        self.texts.append(message.content)
        yield NormalizedEvent(kind="text_delta", text="ok")
        yield NormalizedEvent(
            kind="result",
            result=RunResult(text="ok", session_id="thr", usage={}),
        )


def _live_turn(text="hi"):
    return TurnEnvelope(text=text, response=asyncio.get_running_loop().create_future())


def test_denied_turn_creates_no_backend_turn(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        await inbox.put(_live_turn("held"))
        await inbox.put(ShutdownEnvelope())
        await _drain_codex_inbox(
            backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
            print_stream=False, name="agent", host=None,
            shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
        )

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == []


def test_held_envelope_fails_honestly_at_shutdown(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    outcomes = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        env = _live_turn("held")
        await inbox.put(env)
        await inbox.put(ShutdownEnvelope())
        await _drain_codex_inbox(
            _ScriptedBackend(), inbox, state_dir=tmp_path, pid=1, stop=stop,
            print_stream=False, name="agent", host=None,
            shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
        )
        try:
            env.response.result()
        except BaseException as exc:  # noqa: BLE001 — asserting the failure shape
            outcomes.append(type(exc).__name__)

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == ["QuotaBlockedError"]


def test_blocked_turns_create_no_backend_turn(tmp_path):
    # Arrange — the driver is ALREADY running while the route is blocked.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        await inbox.put(_live_turn("first"))
        await inbox.put(_live_turn("second"))
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        try:
            for _ in range(300):
                if inbox.empty():
                    break
                await asyncio.sleep(0.01)
        finally:
            stop.set()
            await asyncio.wait_for(drain, timeout=5.0)

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == []


def test_blocked_to_clear_transition_without_new_message(tmp_path):
    # Arrange — the driver is ALREADY running when the clear lands.
    # The long-lived driver parks until shutdown; what matters is that
    # BOTH held response futures complete (in order, no backend burn
    # while blocked) on clear alone, with no new arrival to wake the
    # queue. Shutdown then ends the run; cleanup retrieves exceptions.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()
    results: list = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        first = _live_turn("first")
        second = _live_turn("second")
        await inbox.put(first)
        await inbox.put(second)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        pending = None
        try:
            for _ in range(300):
                if backend.texts == [] and inbox.empty():
                    break
                await asyncio.sleep(0.01)
            # Act: clear with NO new arrival — the recheck tick must notice.
            # (Nothing runs while blocked: covered by companion control.)
            clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
            pending = asyncio.gather(first.response, second.response)
            answers = await asyncio.wait_for(asyncio.shield(pending), timeout=5.0)
            results.append(tuple(answers))
        finally:
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(drain, timeout=5.0)
            except TimeoutError:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            for env in (first, second):
                if env.response.done() and not env.response.cancelled():
                    env.response.exception()

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == ["first", "second"]


def test_cleared_held_turns_resolve_in_order(tmp_path):
    # Arrange — the driver is ALREADY running when the clear lands.
    # The long-lived driver parks until shutdown; what matters is that
    # BOTH held response futures complete on clear alone, with no new
    # arrival to wake the queue. Shutdown then ends the run; cleanup
    # retrieves exceptions.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()
    results: list = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        first = _live_turn("first")
        second = _live_turn("second")
        await inbox.put(first)
        await inbox.put(second)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        pending = None
        try:
            for _ in range(300):
                if backend.texts == [] and inbox.empty():
                    break
                await asyncio.sleep(0.01)
            # Act: clear with NO new arrival — the recheck tick must notice.
            # (Nothing runs while blocked: covered by companion control.)
            clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
            pending = asyncio.gather(first.response, second.response)
            answers = await asyncio.wait_for(asyncio.shield(pending), timeout=5.0)
            results.append(tuple(answers))
        finally:
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(drain, timeout=5.0)
            except TimeoutError:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            for env in (first, second):
                if env.response.done() and not env.response.cancelled():
                    env.response.exception()

    # Act
    _limited(_scenario())
    # Assert
    assert results == [("ok", "ok")]


def test_shutdown_with_live_incident_fails_held_honestly(tmp_path):
    # Arrange — shutdown arrives while the incident is STILL live and
    # turns are held: the run ends and held envelopes fail honestly
    # (never completed, never dropped, never run against the backend).
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()
    outcomes: list = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        env = _live_turn("held")
        await inbox.put(env)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        await asyncio.sleep(0.2)
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(drain, timeout=5.0)
        try:
            env.response.result()
        except BaseException as exc:  # noqa: BLE001 — asserting the failure shape
            outcomes.append(type(exc).__name__)

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == ["QuotaBlockedError"]


def test_shutdown_with_live_incident_creates_no_backend_turn(tmp_path):
    # Arrange — shutdown arrives while the incident is STILL live.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    backend = _ScriptedBackend()

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        await inbox.put(_live_turn("held"))
        drain = asyncio.create_task(
            _drain_codex_inbox(
                backend, inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        await asyncio.sleep(0.2)
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(drain, timeout=5.0)

    # Act
    _limited(_scenario())
    # Assert
    assert backend.texts == []


def test_cancel_conserves_held_envelopes(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    outcomes = []

    async def _scenario():
        inbox: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        env = _live_turn("held")
        await inbox.put(env)
        drain = asyncio.create_task(
            _drain_codex_inbox(
                _ScriptedBackend(), inbox, state_dir=tmp_path, pid=1, stop=stop,
                print_stream=False, name="agent", host=None,
                shutdown_type=ShutdownEnvelope, turn_type=TurnEnvelope,
            )
        )
        await asyncio.sleep(0.2)
        drain.cancel()
        try:
            await drain
        except asyncio.CancelledError:
            pass
        try:
            env.response.result()
        except BaseException as exc:  # noqa: BLE001 — asserting conservation
            outcomes.append(type(exc).__name__)

    # Act
    _limited(_scenario())
    # Assert
    assert outcomes == ["QuotaBlockedError"]


def test_daemon_preserves_blocked_across_ticks(tmp_path):
    # Arrange — a LIVE incident backs the BLOCKED beat.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    write_heartbeat(tmp_path, pid=1, state=STATE_BLOCKED, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    states = [_limited(_tick(decider)) for _ in range(2)]
    # Assert
    assert states == [STATE_BLOCKED, STATE_BLOCKED]


def test_daemon_does_not_preserve_stale_blocked_without_incident(tmp_path):
    # Arrange — a BLOCKED beat with NO live incident is stale testimony
    # (e.g. written before a verified clear); the durable file rules.
    write_heartbeat(tmp_path, pid=1, state=STATE_BLOCKED, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_READY


async def _tick(decider):
    return decider()


def test_daemon_reads_durable_incident_after_foreign_tick(tmp_path):
    # Arrange — the periodic writer overwrote the beat; the file still rules.
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    write_heartbeat(tmp_path, pid=1, state=STATE_READY, name="a", host=None, writer="periodic")
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_BLOCKED


def test_daemon_returns_ready_after_verified_clear(tmp_path):
    # Arrange
    record_quota_incident(tmp_path, route=_ROUTE, detail="wall", at=_TIME)
    clear_quota_incident(tmp_path, route=_ROUTE, evidence=_good_evidence())
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_READY


def test_daemon_still_preserves_busy(tmp_path):
    # Arrange
    write_heartbeat(tmp_path, pid=1, state=STATE_BUSY, name="a", host=None, writer=WRITER_TURN_DRIVER)
    decider = make_daemon_state_fn(
        tmp_path, stop=asyncio.Event(), convo_ref={"task": SimpleNamespace(done=lambda: False)}
    )
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_BUSY


def test_daemon_reports_ready_without_task(tmp_path):
    # Arrange
    decider = make_daemon_state_fn(tmp_path, stop=asyncio.Event(), convo_ref={})
    # Act
    got = _limited(_tick(decider))
    # Assert
    assert got == STATE_READY


def test_malformed_null_route_record_denies_admission(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text(
        json.dumps({"version": 1, "routes": {_ROUTE: None}}), encoding="utf-8"
    )
    # Act
    got = quota_admits(tmp_path, _ROUTE)
    # Assert
    assert got is False


def test_string_false_cleared_flag_denies_admission(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text(
        json.dumps({"version": 1, "routes": {_ROUTE: {"cleared": "false"}}}),
        encoding="utf-8",
    )
    # Act
    got = quota_admits(tmp_path, _ROUTE)
    # Assert
    assert got is False


def test_record_on_corrupt_store_writes_nothing(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text("{partial", encoding="utf-8")
    # Act
    got = record_quota_incident(tmp_path, route=_ROUTE, detail="cap", at=_TIME)
    # Assert
    assert got is None


def test_record_on_corrupt_store_keeps_other_route_denied(tmp_path):
    # Arrange
    (tmp_path / "quota_incident.json").write_text("{partial", encoding="utf-8")
    record_quota_incident(tmp_path, route=_ROUTE, detail="cap", at=_TIME)
    # Act
    got = quota_admits(tmp_path, "other")
    # Assert
    assert got is False


def test_quota_error_writes_incident_cause_to_durable_store(tmp_path):
    # Arrange
    session = _ErrorSession(_cap_error_event())
    # Act
    _pump_error_turn(tmp_path, _cap_error_event(), session=session)
    # Assert
    assert json.loads((tmp_path / "quota_incident.json").read_text())["routes"][
        _ROUTE
    ]["cause"] == "quota-exhausted"
