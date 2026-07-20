#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postcondition check for ``sac agents restart`` — did the run CYCLE?

A restart's contract is not "the call returned"; it is "the agent is now
a DIFFERENT run than it was". Until this module existed the CLI had no
way to tell those apart: the exit code was byte-identical between a real
restart and one that touched nothing, and the console printed the same
green ``Agent 'x' restarted`` line for both (P0, 2026-07-20 — an
in-container ``sac agents restart scitex-cards`` reported success while
the target's pid and tmux session were unchanged 70 minutes later).

IDENTITY-OF-RUN
---------------

``<runtime-dir>/<agent>/instance_id`` is a uuid7 minted by
``_state.state_db.record_instance_start`` at launch, written by
``_lifecycle._instances.record_local_instance`` (synchronously, before
``agent_start`` returns) and deleted by ``end_local_instance`` on stop.
It therefore names THE RUN, not the agent: a cycled agent has a new one,
an agent that never cycled has the old one, and a stopped agent has none.

TERNARY, NEVER BINARY
---------------------

The verdict has THREE states, because "I could not see" is a real answer
and collapsing it into either pole is a lie:

  * ``True``  — a run exists now and it is NOT the run we saw before.
  * ``False`` — we saw a run before and it is either still that same run
    (nothing cycled) or gone entirely (the restart left the agent DOWN).
    Both are definitive: we HELD the before-evidence.
  * ``None``  — no marker before AND none after. No evidence either way.
    The caller must leave its own verdict untouched; inventing a FAILURE
    here is exactly the mirror of the false SUCCESS this module exists to
    kill, and would be equally misleading.

WHERE THIS RUNS
---------------

On the process that PERFORMS the restart. Inside an apptainer SIF the
runtime dir resolves to the container's own ``$HOME`` (``/home/agent``),
which does NOT hold the host fleet's state — a container-side probe of a
host agent reads an empty directory and could only ever return ``None``.
So the in-SIF path does not re-derive this verdict; it brokers the
restart to the host and RELAYS the host CLI's own ``verified`` field out
of the JSON envelope (see ``_restart_remote.brokered_restart``). Evidence
is produced where the evidence lives.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RestartVerdict",
    "read_run_identity",
    "verify_cycled",
]


@dataclass(frozen=True)
class RestartVerdict:
    """The postcondition verdict plus the evidence it was drawn from.

    ``verified`` is the ternary described in the module docstring;
    ``reason`` is a short operator-facing sentence naming WHY, and
    ``before``/``after`` carry the raw identity-of-run values so a
    ``--json`` consumer can re-check the reasoning without re-running
    anything.
    """

    verified: bool | None
    reason: str
    before: str | None
    after: str | None

    def as_dict(self) -> dict:
        """JSON-envelope fields for this verdict (flat, prefixed)."""
        return {
            "verified": self.verified,
            "verified_reason": self.reason,
            "run_before": self.before,
            "run_after": self.after,
        }


def read_run_identity(name: str) -> str | None:
    """Return ``name``'s current identity-of-run, or ``None`` if it has no run.

    Reads ``<runtime-dir>/<name>/instance_id`` through the same resolvers
    the lifecycle uses (``_session_reset._runtime_state_dir`` +
    ``_runners._session_state.read_instance_id``), so a relocated
    ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` is honoured and there is no
    second, drifting notion of where the marker lives. Never raises: a
    missing dir, a missing file and an unreadable file are all "no run",
    which the ternary in :func:`verify_cycled` handles explicitly.
    """
    from ..._lifecycle._session_reset import _runtime_state_dir
    from ..._runners._session_state import read_instance_id

    return read_instance_id(_runtime_state_dir(name))


def verify_cycled(name: str, before: str | None, after: str | None) -> RestartVerdict:
    """Decide whether ``name`` actually cycled between ``before`` and ``after``.

    See the module docstring for the ternary contract. The four cases are
    written out one per branch, in evidence order, so the decision reads
    as a table rather than as a chain of conditions.
    """
    if after is not None and after != before:
        return RestartVerdict(
            True,
            f"agent {name!r} is a NEW run ({before or 'no run'} -> {after})",
            before,
            after,
        )
    if before is not None and after == before:
        return RestartVerdict(
            False,
            f"agent {name!r} did NOT cycle: it is still run {before} — the "
            f"restart changed nothing, so it is the OLD process on its OLD "
            f"credentials",
            before,
            after,
        )
    if before is not None and after is None:
        return RestartVerdict(
            False,
            f"agent {name!r} has NO run after the restart (was {before}) — "
            f"the stop leg ran but nothing came back up",
            before,
            after,
        )
    return RestartVerdict(
        None,
        f"agent {name!r} had no instance_id marker before OR after, so the "
        f"restart could not be verified either way (no evidence — this is "
        f"NOT a reported failure)",
        before,
        after,
    )
