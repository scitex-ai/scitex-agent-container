"""LOAD-BEARING: a slow / failed / ABANDONED heartbeat tick is UNKNOWN — never "dead".

This is the rule whose violation broke fleet agent-to-agent comms.

The listen heartbeat loops probed agents SERIALLY (``tui_heartbeat_loop``:
3 ``tmux`` subprocess spawns per agent; ``sdk_heartbeat_loop``: one runtime
probe per agent). At fleet scale on a loaded host the tick exceeded its 30s
budget and the ``off_loop`` watchdog ABANDONED it, so NO liveness data was
written. The registry went stale, LIVE agents recorded as "stopped", and
``agent_send`` then REFUSED to deliver to them (proven on ``scitex-hpc``:
tmux session alive and its a2a port LISTENING, while the registry said
``status=stopped, a2a_port=null``).

The cost fix is the batched probe (see ``_tmux_probe.list_sessions_activity``
and the loops' tick bodies). THIS file pins the correctness invariant that
must hold even when a tick still cannot complete:

  * A failed probe yields UNKNOWN (``None``), never an empty fleet. The loop
    writes NOTHING and the previous heartbeat survives byte-for-byte.
  * The loops NEVER write a "dead"/"stopped" verdict — they only ever write
    fresh "running" beats. Absence of a beat is absence of evidence.
  * A dropped tick leaves the two sources ``agent_send`` resolves an endpoint
    from — the active ``instances`` row and the durable ``a2a_ports`` claim —
    completely untouched, so a live agent stays reachable.
  * An abandoned tick's thread is never joined, so the loops refuse to stack
    a second tick body on top of one still in flight (that pile-up saturated
    the SHARED executor ``agent_restart`` / ``host_exec`` also dispatch on).

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

from scitex_agent_container._lifecycle._sdk_heartbeat_loop import sdk_heartbeat_loop
from scitex_agent_container._lifecycle._tui_heartbeat_loop import tui_heartbeat_loop
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container._state import port_allocator
from scitex_agent_container._state.state_db import (
    list_active_instances,
    record_instance_start,
)

PINNED_ACTIVITY_TS = 1_750_000_000
LIVE_AGENT = "scitex-hpc"
LIVE_PORT = 19019


class _Cfg:
    """Minimal AgentConfig stand-in — the injected probe seam only needs an
    object to hand back."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.runtime = "claude-agent-sdk"


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


def _sdk_lister(state_dir: Path):
    def _lister():
        return [
            {"name": LIVE_AGENT, "config": _Cfg(LIVE_AGENT), "state_dir": state_dir}
        ]

    return _lister


async def _drive(coro_task, settle_s: float = 0.3, until=None) -> None:
    """Let the loop run, then cancel it cleanly.

    ``until`` polls for a positive end-state (the tick body runs in an
    executor thread, so a fixed sleep races it on a loaded host). Without
    it a fixed wait is correct — those tests assert that NOTHING changed,
    and waiting longer only strengthens them.
    """
    if until is None:
        await asyncio.sleep(settle_s)
    else:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not until():
            await asyncio.sleep(0.02)
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


# A tick budget far shorter than WEDGE_S, so the off_loop watchdog really
# does ABANDON the tick — the production failure, reproduced in ms rather
# than by waiting out the 15s floor. (The probe thread is deliberately never
# joined, so keep WEDGE_S short enough that loop teardown stays fast.)
WEDGE_S = 0.5
TICK_BUDGET_S = 0.05


def _wedged_probe(*_args, **_kwargs):
    """A probe that outlives its tick budget — the loaded-host case that
    blew the 30s budget and got the tick ABANDONED."""
    time.sleep(WEDGE_S)
    return {}


# ---------------------------------------------------------------------------
# TUI loop — failed probe is UNKNOWN, not an empty fleet.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_failed_probe_preserves_previous_heartbeat_bytes(tmp_path: Path):
    # Arrange — live agent with a good beat; the batched probe then FAILS.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=lambda: None,  # probe failed → UNKNOWN
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    await _drive(task, settle_s=0.15)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — last-known-good survived byte-for-byte.
    assert after == before


@pytest.mark.asyncio
async def test_tui_failed_probe_never_writes_a_dead_state(tmp_path: Path):
    # Arrange — the loop must never record a dead/stopped verdict.
    state_dir = _seeded_live_agent(tmp_path)
    # Act
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=lambda: None,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
        )
    )
    await _drive(task, settle_s=0.15)
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert — still "running", NOT flipped to dead.
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_tui_abandoned_tick_preserves_the_live_agents_heartbeat(tmp_path: Path):
    # Arrange — a probe wedged past the tick budget: the off_loop guard
    # ABANDONS the tick exactly as it does in production.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=_wedged_probe,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
            tick_timeout_s=TICK_BUDGET_S,
        )
    )
    await _drive(task)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — the abandoned tick wrote nothing and erased nothing.
    assert after == before


@pytest.mark.asyncio
async def test_tui_slow_tick_does_not_stack_a_second_probe_thread(tmp_path: Path):
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
# SDK loop — a wedged runtime probe is UNKNOWN for that agent, not dead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_wedged_probe_preserves_previous_heartbeat_bytes(tmp_path: Path):
    # Arrange — live agent with a good beat; its runtime probe then wedges.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act — per-probe timeout fires → UNKNOWN → no beat written.
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_sdk_lister(state_dir),
            is_running_fn=_wedged_probe,
            write_fn=write_heartbeat,
            probe_timeout_s=0.05,
        )
    )
    await _drive(task, settle_s=0.3)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — the wedged probe left last-known-good untouched.
    assert after == before


@pytest.mark.asyncio
async def test_sdk_wedged_probe_never_writes_a_dead_state(tmp_path: Path):
    # Arrange
    state_dir = _seeded_live_agent(tmp_path)
    # Act
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_sdk_lister(state_dir),
            is_running_fn=_wedged_probe,
            write_fn=write_heartbeat,
            probe_timeout_s=0.05,
        )
    )
    await _drive(task, settle_s=0.3)
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert — still "running", NOT flipped to dead.
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_sdk_one_wedged_probe_does_not_starve_a_healthy_agent(tmp_path: Path):
    # Arrange — the whole point of the bounded parallel pool: agent A's
    # wedged probe must not stop agent B from getting its beat (serially it
    # held the tick until the budget blew and EVERYTHING was abandoned).
    wedged_dir = tmp_path / "wedged"
    wedged_dir.mkdir()
    healthy_dir = tmp_path / "healthy"
    healthy_dir.mkdir()

    def _lister():
        return [
            {"name": "wedged", "config": _Cfg("wedged"), "state_dir": wedged_dir},
            {"name": "healthy", "config": _Cfg("healthy"), "state_dir": healthy_dir},
        ]

    def _probe(cfg):
        if cfg.name == "wedged":
            time.sleep(WEDGE_S)
        return True

    # Act
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_lister,
            is_running_fn=_probe,
            write_fn=write_heartbeat,
            probe_timeout_s=0.1,
        )
    )
    await _drive(task, until=lambda: (healthy_dir / "heartbeat.json").is_file())
    # Assert — the healthy agent still beat despite its wedged neighbour.
    assert (healthy_dir / "heartbeat.json").is_file()


# ---------------------------------------------------------------------------
# REAL state.db: a dropped tick must leave a live agent REACHABLE.
#
# ``_send_resolve.resolve_send_endpoint`` resolves an endpoint from exactly
# two sources: the active ``instances`` row and the durable ``a2a_ports``
# claim. A dropped heartbeat tick must not disturb either — that is what it
# means for "no fresh heartbeat" to be UNKNOWN rather than DEAD.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dropped_tick_leaves_the_live_instance_row_active(tmp_path: Path):
    # Arrange — a REAL state.db with a REAL active instance row for a live
    # agent (the scitex-hpc shape: tmux alive, a2a port listening).
    db_path = tmp_path / "state.db"
    state_dir = _seeded_live_agent(tmp_path)
    record_instance_start(LIVE_AGENT, pid=4242, a2a_port=LIVE_PORT, db_path=db_path)
    # Act — a tick that wedges and gets ABANDONED.
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=_wedged_probe,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
            tick_timeout_s=TICK_BUDGET_S,
        )
    )
    await _drive(task)
    still_active = [
        r for r in list_active_instances(db_path=db_path) if r.get("name") == LIVE_AGENT
    ]
    # Assert — the row agent_send resolves from is STILL ACTIVE (not ended,
    # not swept, not "stopped"). A dropped tick cannot kill a live agent.
    assert len(still_active) == 1


@pytest.mark.asyncio
async def test_dropped_tick_leaves_the_durable_a2a_port_claim_intact(tmp_path: Path):
    # Arrange — a REAL durable port claim (the fallback agent_send uses when
    # the instances row is missing/null-port). It reported a2a_port=null in
    # the incident; a dropped tick must never be the reason.
    db_path = tmp_path / "state.db"
    state_dir = _seeded_live_agent(tmp_path)
    port_allocator.claim_port(LIVE_AGENT, explicit=LIVE_PORT, db_path=db_path)
    # Act — a tick that wedges and gets ABANDONED.
    task = asyncio.create_task(
        tui_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_tui_lister(state_dir),
            sessions_fn=_wedged_probe,
            write_fn=write_heartbeat,
            tmux_check=lambda: True,
            tick_timeout_s=TICK_BUDGET_S,
        )
    )
    await _drive(task)
    resolved = port_allocator.get_port(LIVE_AGENT, db_path=db_path)
    # Assert — the live agent's endpoint is still resolvable.
    assert resolved == LIVE_PORT
