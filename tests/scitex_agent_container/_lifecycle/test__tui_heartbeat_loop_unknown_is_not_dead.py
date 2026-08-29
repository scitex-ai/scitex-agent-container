"""LOAD-BEARING (TUI loop): a failed / ABANDONED tick is UNKNOWN — never "dead".

This is the rule whose violation broke fleet agent-to-agent comms.

``tui_heartbeat_loop`` probed agents SERIALLY at 3 ``tmux`` subprocess spawns
each (``exists`` is one; ``session_activity`` goes through ``_display_field``,
which re-probes ``exists`` and then spawns ``tmux display``). At fleet scale on
a loaded host the tick exceeded its 30s budget and the ``off_loop`` watchdog
ABANDONED it, so NO liveness data was written at all — and for this all-TUI
fleet ``heartbeat.json`` is the only proof-of-life the agents emit.

The COST fix is the batched probe (``_tmux_probe.list_sessions_activity``:
one ``tmux list-sessions`` for the whole fleet). THIS file pins the CORRECTNESS
invariant that must hold even when a tick still cannot complete:

  * A failed probe yields UNKNOWN (``None``), never an empty fleet. The loop
    writes NOTHING and the previous heartbeat survives byte-for-byte.
  * The loop NEVER writes a "dead"/"stopped" verdict — only fresh "running"
    beats. Absence of a beat is absence of evidence, not evidence of death.
  * A dropped tick leaves the two sources ``agent_send`` resolves an endpoint
    from — the active ``instances`` row and the durable ``a2a_ports`` claim —
    completely untouched, so a live agent stays REACHABLE. (Proven failure
    mode on ``scitex-hpc``: tmux alive and its a2a port LISTENING, while the
    registry read ``status=stopped, a2a_port=null``.)
  * An abandoned tick's thread is never joined, so the loop refuses to stack a
    second tick body on one still in flight (that pile-up saturated the SHARED
    executor ``agent_restart`` / ``host_exec`` also dispatch on).

The ``sdk_heartbeat_loop`` half of this invariant lives in the sibling
``test__sdk_heartbeat_loop_unknown_is_not_dead.py``.

NO MOCKS: real ``tmp_path`` state dirs, the real ``write_heartbeat`` writer,
and a real sqlite ``state.db`` with real rows written through the real
``record_instance_start`` / ``claim_port`` APIs.

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._tui_heartbeat_loop import tui_heartbeat_loop
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container._state import port_allocator
from scitex_agent_container._state.state_db import (
    list_active_instances,
    record_instance_start,
)


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield

PINNED_ACTIVITY_TS = 1_750_000_000
LIVE_AGENT = "scitex-hpc"
LIVE_PORT = 19019

# A tick budget far shorter than WEDGE_S, so the off_loop watchdog really does
# ABANDON the tick — the production failure, reproduced in ms rather than by
# waiting out the 15s floor. (The probe thread is deliberately never joined, so
# keep WEDGE_S short enough that loop teardown stays fast.)
WEDGE_S = 0.5
TICK_BUDGET_S = 0.05


def _seeded_live_agent(tmp_path: Path) -> Path:
    """A live agent's state dir with a good heartbeat already on disk."""
    state_dir = tmp_path / LIVE_AGENT
    state_dir.mkdir()
    write_heartbeat(state_dir, pid=0, state="running", ts=float(PINNED_ACTIVITY_TS))
    return state_dir


def _tui_lister(state_dir: Path):
    def _lister():
        return [{"name": LIVE_AGENT, "state_dir": state_dir}]

    return _lister


def _wedged_probe(*_args, **_kwargs):
    """A probe that outlives its tick budget — the loaded-host case that blew
    the 30s budget and got the tick ABANDONED."""
    time.sleep(WEDGE_S)
    return {}


async def _drive(task, settle_s: float = 0.3) -> None:
    """Let the loop run, then cancel it cleanly.

    A fixed wait is correct for these tests: they assert that NOTHING changed,
    so waiting longer only strengthens them.
    """
    await asyncio.sleep(settle_s)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _start_loop(state_dir: Path, sessions_fn, *, budget: float | None = None):
    return asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=sessions_fn,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
            tick_timeout_s=budget,
        )
    )


# ---------------------------------------------------------------------------
# A failed probe is UNKNOWN, not an empty fleet.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_probe_preserves_previous_heartbeat_bytes(tmp_path: Path):
    # Arrange — live agent with a good beat; the batched probe then FAILS.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act
    task = _start_loop(state_dir, lambda: None)
    await _drive(task, settle_s=0.15)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — last-known-good survived byte-for-byte.
    assert after == before


@pytest.mark.asyncio
async def test_failed_probe_never_writes_a_dead_state(tmp_path: Path):
    # Arrange — the loop must never record a dead/stopped verdict.
    state_dir = _seeded_live_agent(tmp_path)
    # Act
    task = _start_loop(state_dir, lambda: None)
    await _drive(task, settle_s=0.15)
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert — still "running", NOT flipped to dead.
    assert payload["state"] == "running"


# ---------------------------------------------------------------------------
# An ABANDONED tick writes nothing and erases nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abandoned_tick_preserves_the_live_agents_heartbeat(tmp_path: Path):
    # Arrange — a probe wedged past the tick budget: the off_loop guard
    # ABANDONS the tick exactly as it does in production.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act
    task = _start_loop(state_dir, _wedged_probe, budget=TICK_BUDGET_S)
    await _drive(task)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — the abandoned tick wrote nothing and erased nothing.
    assert after == before


@pytest.mark.asyncio
async def test_slow_tick_does_not_stack_a_second_probe_thread(tmp_path: Path):
    # Arrange — OVERLAP GUARD: an abandoned tick's thread is never joined, so
    # stacking another would leak worker slots out of the SHARED executor.
    state_dir = _seeded_live_agent(tmp_path)
    inflight = 0
    peak = 0

    def _slow_probe():
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            time.sleep(0.4)
            return {}
        finally:
            inflight -= 1

    # Act — interval far shorter than the probe, so ticks WOULD overlap.
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.02,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=_slow_probe,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    await _drive(task, settle_s=0.6)
    # Assert — at most ONE tick body in flight, ever.
    assert peak == 1


# ---------------------------------------------------------------------------
# REAL state.db: a dropped tick must leave a live agent REACHABLE.
#
# ``_send_resolve.resolve_send_endpoint`` resolves an endpoint from exactly two
# sources: the active ``instances`` row and the durable ``a2a_ports`` claim. A
# dropped heartbeat tick must not disturb either — that is what it MEANS for
# "no fresh heartbeat" to be UNKNOWN rather than DEAD.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_tick_leaves_the_live_instance_row_active(tmp_path: Path):
    # Arrange — a REAL state.db with a REAL active instance row for a live
    # agent (the scitex-hpc shape: tmux alive, a2a port listening).
    db_path = tmp_path / "state.db"
    state_dir = _seeded_live_agent(tmp_path)
    record_instance_start(LIVE_AGENT, pid=4242, a2a_port=LIVE_PORT)
    # Act — a tick that wedges and gets ABANDONED.
    task = _start_loop(state_dir, _wedged_probe, budget=TICK_BUDGET_S)
    await _drive(task)
    still_active = [
        r for r in list_active_instances() if r.get("name") == LIVE_AGENT
    ]
    # Assert — the row agent_send resolves from is STILL ACTIVE (not ended, not
    # swept, not "stopped"). A dropped tick cannot kill a live agent.
    assert len(still_active) == 1


@pytest.mark.asyncio
async def test_dropped_tick_leaves_the_durable_a2a_port_claim_intact(
    tmp_path: Path, pg_schema: str
):
    # Arrange — a REAL durable port claim (the fallback agent_send uses when the
    # instances row is missing/null-port). It reported a2a_port=null in the
    # incident; a dropped tick must never be the reason. The claim lives in
    # PostgreSQL since 2026-08-28, so ``pg_schema`` isolates it where a
    # ``tmp_path`` state.db used to.
    state_dir = _seeded_live_agent(tmp_path)
    port_allocator.claim_port(LIVE_AGENT, explicit=LIVE_PORT)
    # Act — a tick that wedges and gets ABANDONED.
    task = _start_loop(state_dir, _wedged_probe, budget=TICK_BUDGET_S)
    await _drive(task)
    resolved = port_allocator.get_port(LIVE_AGENT)
    # Assert — the live agent's endpoint is still resolvable.
    assert resolved == LIVE_PORT
