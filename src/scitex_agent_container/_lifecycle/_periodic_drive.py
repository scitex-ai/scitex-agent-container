"""Unified periodic-drive lane — autonomous-loop for SDK + TUI agents.

Lead a2a ``4973264a`` / ``12a0d8f6`` / ``3afbc1bd`` (2026-06-14):
operator-doctrine ONE mechanism for both runtimes. A research agent
must continue working (monitor Spartan, resubmit chains, report)
BETWEEN driven turns — it can't sit idle waiting for the next a2a
inbound or operator nudge.

DESIGN
------

Mechanism: a periodic-drive tick enqueues a system-message turn
into each running agent's a2a inbox at a configurable interval.
The runtimes consume that inbox uniformly:

* ``ClaudeSessionRuntime`` (SDK)  — sidecar / SDK loop picks up
  the message + injects it as the next user turn.
* ``TuiSessionRuntime`` (TUI)     — sidecar reads the inbox and
  calls ``runtime.send_turn(config, content)`` which lands the
  text via ``mux.send_text_and_submit`` (proven on the host
  2026-06-14).

No TUI-only special case; no SDK-only special case. Same envelope,
same delivery surface (a2a inbox), one switch.

CONTENT
-------

Each periodic-drive turn carries — generated MECHANICALLY from
the agent's spec + ``state.db``, NEVER hand-written, per
operator doctrine 12a0d8:

* The agent's STANDING RULES (from ``spec.rules`` / ``spec.mission``).
* The agent's CURRENT MISSION (spec-derived).
* The agent's CURRENT WORK signal (active git worktree name from
  ``git worktree list`` against ``config.workdir``, last commit,
  branch). Same shape AgentCard's current_work field uses (lead
  a2a 1ada9fda — see ``cli_pkg/_helpers/_agent_list.py``).

The recipient agent's prompt logic decides what to do with the
``kind="periodic_drive"`` envelope (typically: re-read its
state, decide next action, act).

OPT-OUT
-------

Per-agent via ``spec.periodic_drive.enabled: false`` (default
true). Operator-side global disable via env var
``SAC_PERIODIC_DRIVE_DISABLED=1`` for fleet emergencies.

INTERVAL
--------

Default 600s (10 min) — slow enough to never noise an
already-busy agent (each drive turn passes through the same
inbox-dedupe as any a2a inbound; consecutive drives within the
same poll interval coalesce); fast enough for research-agent
autonomy. Per-agent override via
``spec.periodic_drive.interval_s``.

FAILURE MODE
------------

Per-agent emit failure logs + skips that tick; the OTHER
agents in the fleet still drive. Total daemon failure logs +
the sac listen process keeps serving inbound a2a + the
heartbeat loop — the drive lane degrades gracefully.

WHY NOT A NEW DAEMON
--------------------

The ``sac listen`` heartbeat loop already runs at a 60s
cadence; we add the drive-tick check INTO that loop. No
extra systemd unit, no new pid file, no daemon supervisor
to monitor.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# Env-var escape hatch for fleet-wide pause without a redeploy.
_DISABLE_ENV = "SAC_PERIODIC_DRIVE_DISABLED"

# Default tick interval per agent. Long enough that an actively
# working agent's "next action" loop has time to run between
# drives; short enough to keep an idle agent from drifting more
# than ~10 min.
DEFAULT_INTERVAL_S = 600.0

# Envelope kind discriminator the recipient agent's prompt logic
# matches on.
ENVELOPE_KIND = "periodic_drive"


@dataclass(frozen=True)
class PeriodicDriveEnvelope:
    """One drive tick's content for a single agent.

    Returned by :func:`build_envelope` so the test suite can assert
    on the rendered shape without re-deriving from state.db.
    """

    agent_name: str
    kind: str
    body: str
    generated_at: float


@dataclass
class _AgentState:
    """Minimal runtime view of an agent the drive lane needs.

    A real class (no MagicMock / monkeypatch) so the test suite
    can inject deterministic instances. Production code resolves
    this from ``state.db`` + the loaded ``AgentConfig``.
    """

    name: str
    is_running: bool
    workdir: str
    branch: str
    last_commit_subject: str
    worktree_name: str
    standing_rules: str
    mission: str
    last_drive_at: float = 0.0
    interval_s: float = DEFAULT_INTERVAL_S
    enabled: bool = True


def build_envelope(
    state: _AgentState, *, now: float | None = None
) -> PeriodicDriveEnvelope:
    """Render the drive turn's body from an :class:`_AgentState`.

    Strict separator-of-concerns: this function does NOT read
    disk or git — the caller passes the already-resolved fields.
    Lets the unit suite assert on the rendered shape without
    real git / real state.db.
    """
    when = now if now is not None else time.time()
    body = (
        f"[sac periodic drive — {state.name}]\n"
        f"\n"
        f"## Standing rules\n"
        f"{state.standing_rules}\n"
        f"\n"
        f"## Current mission\n"
        f"{state.mission}\n"
        f"\n"
        f"## Current work (state.db + git)\n"
        f"- workdir: {state.workdir}\n"
        f"- branch: {state.branch}\n"
        f"- active worktree: {state.worktree_name}\n"
        f"- last commit subject: {state.last_commit_subject}\n"
        f"\n"
        f"## Action\n"
        f"Re-read your state above. Decide the next concrete\n"
        f"action toward your mission and execute it. If you are\n"
        f"already mid-task, continue. If you are idle waiting on\n"
        f"a remote (Spartan / CI / lead), check + report status.\n"
        f"If your mission is complete, signal lead.\n"
    )
    return PeriodicDriveEnvelope(
        agent_name=state.name,
        kind=ENVELOPE_KIND,
        body=body,
        generated_at=when,
    )


def is_globally_disabled(env: dict[str, str] | None = None) -> bool:
    """True iff ``SAC_PERIODIC_DRIVE_DISABLED=1`` is in the env.

    Operator-side fleet-emergency pause — short-circuits every
    tick without a redeploy.
    """
    env = env if env is not None else dict(os.environ)
    return env.get(_DISABLE_ENV, "").strip() == "1"


def should_drive(state: _AgentState, *, now: float | None = None) -> bool:
    """Decision predicate: should we enqueue a drive tick for
    this agent right now?

    Honours:
      * ``state.enabled`` (per-agent opt-out).
      * ``state.is_running`` (don't drive a stopped agent).
      * ``state.last_drive_at + state.interval_s <= now``
        (rate-limit per-agent — never noisier than the interval).
    """
    if not state.enabled:
        return False
    if not state.is_running:
        return False
    when = now if now is not None else time.time()
    return state.last_drive_at + state.interval_s <= when


def sweep(
    agents: Iterable[_AgentState],
    *,
    emit: Callable[[PeriodicDriveEnvelope], None],
    now: float | None = None,
) -> list[PeriodicDriveEnvelope]:
    """Iterate the fleet; emit a drive envelope for each due agent.

    ``emit`` is the side-effecting hook — the production callsite
    binds it to the a2a inbox writer (`mcp__sac__a2a_send` from the
    daemon's own identity, or a direct ``send_message_to`` against
    the agent's listen endpoint). Tests pass a list-appender to
    capture envelopes for assertion.

    Returns the list of envelopes actually emitted this tick so
    the caller can record telemetry / structural_alerts on
    individual emit failures (each agent's emit is wrapped in
    its own try/except — one bad agent doesn't block the fleet).
    """
    if is_globally_disabled():
        logger.info("periodic_drive: SAC_PERIODIC_DRIVE_DISABLED=1 → skipping tick")
        return []
    emitted: list[PeriodicDriveEnvelope] = []
    when = now if now is not None else time.time()
    for state in agents:
        try:
            if not should_drive(state, now=when):
                continue
            envelope = build_envelope(state, now=when)
            emit(envelope)
            emitted.append(envelope)
            logger.info(
                "periodic_drive: emitted to %s (interval=%.0fs, last=%.0f)",
                state.name,
                state.interval_s,
                state.last_drive_at,
            )
        except Exception as exc:  # stx-allow: fallback (per-agent best-effort — one bad agent does not block the fleet sweep)
            logger.warning("periodic_drive: emit failed for %s: %s", state.name, exc)
    return emitted


__all__ = [
    "DEFAULT_INTERVAL_S",
    "ENVELOPE_KIND",
    "PeriodicDriveEnvelope",
    "build_envelope",
    "is_globally_disabled",
    "should_drive",
    "sweep",
]
