"""LOAD-BEARING (SDK loop): a wedged probe is UNKNOWN for that agent — never "dead".

Sibling of ``test__tui_heartbeat_loop_unknown_is_not_dead.py``, which carries
the same invariant for the TUI loop plus the real-``state.db`` reachability
tests. See that module for the full incident context.

``sdk_heartbeat_loop`` probed agents SERIALLY, so ONE wedged runtime probe (a
stalled pidfile read on a loaded host) held the entire tick until it blew its
budget and the ``off_loop`` watchdog ABANDONED it — writing NO beats for ANY
agent. The COST fix is the bounded parallel pool with a per-probe timeout
(mirroring ``get_agent_list_data``). THIS file pins the CORRECTNESS invariant:

  * A probe that times out yields UNKNOWN for THAT agent — no beat is written,
    its previous heartbeat is retained. The loop never records a "dead" verdict
    and never erases an existing beat.
  * One wedged agent must not starve its healthy neighbours (serially it took
    the whole tick down with it).

NO MOCKS: real ``tmp_path`` state dirs and the real ``write_heartbeat`` writer,
driven through the loop's injection seams.

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._sdk_heartbeat_loop import sdk_heartbeat_loop
from scitex_agent_container._runners._session_state import write_heartbeat

PINNED_ACTIVITY_TS = 1_750_000_000
LIVE_AGENT = "scitex-hpc"

# The probe outlives its per-probe timeout, so the pool really does time it out.
WEDGE_S = 0.5
PROBE_TIMEOUT_S = 0.05


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


def _sdk_lister(state_dir: Path):
    def _lister():
        return [
            {"name": LIVE_AGENT, "config": _Cfg(LIVE_AGENT), "state_dir": state_dir}
        ]

    return _lister


def _wedged_probe(*_args, **_kwargs):
    """A probe that outlives its per-probe timeout (the stalled-FS case)."""
    time.sleep(WEDGE_S)
    return True


async def _drive(task, settle_s: float = 0.3, until=None) -> None:
    """Let the loop run, then cancel it cleanly.

    ``until`` polls for a positive end-state (the tick body runs in an executor
    thread, so a fixed sleep races it on a loaded host). Without it a fixed wait
    is correct — those tests assert NOTHING changed, so waiting only strengthens
    them.
    """
    if until is None:
        await asyncio.sleep(settle_s)
    else:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not until():
            await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# A wedged runtime probe is UNKNOWN for that agent, not dead.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wedged_probe_preserves_previous_heartbeat_bytes(tmp_path: Path):
    # Arrange — live agent with a good beat; its runtime probe then wedges.
    state_dir = _seeded_live_agent(tmp_path)
    before = (state_dir / "heartbeat.json").read_bytes()
    # Act — the per-probe timeout fires → UNKNOWN → no beat written.
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_sdk_lister(state_dir),
            is_running_fn=_wedged_probe,
            write_fn=write_heartbeat,
            probe_timeout_s=PROBE_TIMEOUT_S,
        )
    )
    await _drive(task)
    after = (state_dir / "heartbeat.json").read_bytes()
    # Assert — the wedged probe left last-known-good untouched.
    assert after == before


@pytest.mark.asyncio
async def test_wedged_probe_never_writes_a_dead_state(tmp_path: Path):
    # Arrange — the loop must never record a dead/stopped verdict.
    state_dir = _seeded_live_agent(tmp_path)
    # Act
    task = asyncio.create_task(
        sdk_heartbeat_loop(
            interval_s=0.05,
            agent_lister=_sdk_lister(state_dir),
            is_running_fn=_wedged_probe,
            write_fn=write_heartbeat,
            probe_timeout_s=PROBE_TIMEOUT_S,
        )
    )
    await _drive(task)
    payload = json.loads((state_dir / "heartbeat.json").read_text(encoding="utf-8"))
    # Assert — still "running", NOT flipped to dead.
    assert payload["state"] == "running"


@pytest.mark.asyncio
async def test_one_wedged_probe_does_not_starve_a_healthy_agent(tmp_path: Path):
    # Arrange — the whole point of the bounded parallel pool: agent A's wedged
    # probe must not stop agent B from getting its beat (serially it held the
    # tick until the budget blew and EVERY agent's beat was abandoned).
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
