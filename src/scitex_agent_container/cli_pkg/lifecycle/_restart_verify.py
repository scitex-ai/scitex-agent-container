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

THE LEDGER IS NECESSARY, NEVER SUFFICIENT
-----------------------------------------

The marker above is written by the same start path whose work we are
checking, so on its own it is not evidence — it is an ECHO. Reading it
alone is how this module's own contract got broken: MEASURED 2026-08-14
on scitex-compute-04, ``sac agents restart`` printed ``verified: agent
'scitex-agent-container' is a NEW run (c06d2fec-… -> 67068008-…)`` while
the agent's tmux session (``tui-scitex-agent-container``) had been alive
and untouched since 11:22 the previous day. Three ``instances`` rows had
been minted that night; every one carried ``screen`` NULL and a pid
matching no live process. Nothing in the restart had ever ASKED THE OS
anything — and because the maintenance path then refuses with
``agent_not_running``, the corrupt container venv it was trying to repair
could not be repaired by sac at all. The only sequence that worked was a
hand-run ``tmux kill-session``, then ``stop``, then ``start``.

So a ``True`` now requires TWO independent witnesses:

  1. the LEDGER says the identity-of-run changed, and
  2. the OS says the agent's multiplexer session changed with it —
     ``#{session_created}``, the one tmux stamp that is constant for the
     life of a session and different for the next one.

Witness 2 is read through ``instances.screen``, which
:func:`_lifecycle._instances.record_local_instance` fills with the name
the runtime passed to ``tmux new-session -s``. A row that does not name a
session cannot be checked against anything, and the honest answer there
is "cannot verify" — NOT "verified".

TERNARY, NEVER BINARY
---------------------

The verdict has THREE states, because "I could not see" is a real answer
and collapsing it into either pole is a lie:

  * ``True``  — a run exists now, it is NOT the run we saw before, AND
    the session the OS reports for it is a different session than before.
  * ``False`` — we saw a run before and it is either still that same run
    (nothing cycled), gone entirely (the restart left the agent DOWN), or
    the ledger minted a new id over a session the OS says never moved.
    All three are definitive: we HELD the before-evidence.
  * ``None``  — no evidence either way: no marker before AND none after,
    or a ledger that claims a new run while no process observation could
    be taken (``screen`` NULL, a wedged tmux, a container's namespace).
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
from typing import Callable

__all__ = [
    "RestartVerdict",
    "SessionObservation",
    "read_run_identity",
    "read_session_identity",
    "verify_cycled",
]


@dataclass(frozen=True)
class SessionObservation:
    """Did we LOOK at the agent's multiplexer session, and what did we see?

    Three states, and the caller must be able to tell all three apart:

      * ``observed=True``, ``identity="tui-x@1755000000"`` — a probe that
        could see this host's tmux ran, and the session is THERE, with
        that birthday.
      * ``observed=True``, ``identity=None`` — the same probe ran and the
        session is CONFIRMED ABSENT.
      * ``observed=False`` — WE COULD NOT LOOK. ``blind_because`` says
        why. This is the state the whole module turns on: it must never
        be collapsed into "absent", because "I could not see the session"
        and "the session is not there" authorise opposite conclusions.

    The default constructs the blind state on purpose, so a caller that
    forgets to pass an observation gets an abstention rather than a
    silently-unchecked pass.
    """

    observed: bool = False
    identity: str | None = None
    blind_because: str = "no process observation was taken"


@dataclass(frozen=True)
class RestartVerdict:
    """The postcondition verdict plus the evidence it was drawn from.

    ``verified`` is the ternary described in the module docstring;
    ``reason`` is a short operator-facing sentence naming WHY,
    ``before``/``after`` carry the raw identity-of-run values, and
    ``session_before``/``session_after`` carry the OS-side session
    identities — so a ``--json`` consumer can re-check the reasoning,
    from BOTH witnesses, without re-running anything.
    """

    verified: bool | None
    reason: str
    before: str | None
    after: str | None
    session_before: str | None = None
    session_after: str | None = None

    def as_dict(self) -> dict:
        """JSON-envelope fields for this verdict (flat, prefixed)."""
        return {
            "verified": self.verified,
            "verified_reason": self.reason,
            "run_before": self.before,
            "run_after": self.after,
            "session_before": self.session_before,
            "session_after": self.session_after,
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


def _created_snapshot(*, socket_name: str | None = None) -> dict | None:
    """``{session: created_epoch}`` for every session, or ``None`` if blind."""
    from ..._runners._tmux._tmux_probe import list_sessions_created

    return list_sessions_created(socket_name=socket_name)


def recorded_session_name(
    name: str,
    *,
    in_sif_fn: Callable[[], bool] | None = None,
) -> str | None:
    """The multiplexer session ``name``'s newest ``instances`` row names.

    ``instances.screen``, read through :func:`_state.state_db
    .last_known_instance` so an ENDED row still answers — a row that says
    "stopped" while its session is demonstrably alive is precisely the
    disagreement this module has to be able to see.

    ``None`` when the agent has no row, when the row predates
    :func:`_lifecycle._instances.record_local_instance` filling the column
    (``screen`` NULL), or when that session is not ours to look at — a row
    written on ANOTHER HOST, or a caller sitting inside a container, whose
    tmux is a different namespace from the host's. In both of those the
    name would be probed against the wrong server, and an answer from the
    wrong server is not a weaker answer, it is not an answer.

    ``in_sif_fn`` is the injection seam (a real callable; see
    :func:`_lifecycle._verdict_tmux.pid_namespace_is_observable`) so a
    test can drive BOTH namespace answers deterministically. It has to
    exist: this suite runs inside a SIF while its conftest clears the
    in-SIF markers, so the ambient answer is whatever the harness happens
    to have left behind — a host-dependent test either way. Never raises.
    """
    try:
        from ..._state.state_db import last_known_instance

        row = last_known_instance(name)
    except Exception:  # stx-allow: fallback (an unreadable registry is "we could not look", never a claim about the session)
        return None
    if not row:
        return None
    session = str(row.get("screen") or "").strip()
    if not session:
        return None
    from ..._lifecycle._verdict_tmux import pid_namespace_is_observable

    # Same predicate the liveness probe uses for pids, and for the same
    # reason: a name minted in another machine's (or the host's, when we
    # are in a SIF) namespace is not ours to read. Asked UNCONDITIONALLY,
    # never only when the row names a host: ``row_host=None`` skips just
    # the cross-host arm, and the container arm is the one that must fire
    # for every row — gating the whole predicate on ``row_host`` let an
    # in-SIF caller probe a hostless row's name against the CONTAINER's
    # tmux and read the answer as the host's.
    observable, _why = pid_namespace_is_observable(
        row_host=str(row.get("host") or "") or None,
        in_sif_fn=in_sif_fn,
    )
    if not observable:
        return None
    return session


def read_session_identity(
    name: str,
    *,
    session_name_fn: Callable[[str], str | None] | None = None,
    snapshot_fn: Callable[..., dict | None] | None = None,
) -> SessionObservation:
    """Ask the OS what ``name``'s multiplexer session is RIGHT NOW.

    The second, independent witness (see the module docstring). Returns a
    :class:`SessionObservation` whose ``identity`` pairs the session name
    with its ``#{session_created}`` birthday — the one tmux stamp that
    does not move for a session that was never touched, so comparing it
    across a restart answers exactly the question the ledger cannot.

    Blindness is reported as blindness. A row with no ``screen``, a
    cross-host row, a wedged tmux, or a container that cannot see the
    host's tmux server all come back ``observed=False`` — never as an
    absent session, which would read as a definitive failure.

    ``session_name_fn`` / ``snapshot_fn`` are the injection seams (real
    callables, no mocks): tests drive the RULE without a live fleet.
    """
    session = (session_name_fn or recorded_session_name)(name)
    if not session:
        return SessionObservation(
            False,
            None,
            f"no ``instances`` row names a tmux session for {name!r} "
            f"(``screen`` is NULL, or the row belongs to another host), so "
            f"there is no handle to ask the OS about",
        )
    from ..._lifecycle._verdict_tmux import observed_session_snapshot

    snapshot = observed_session_snapshot(snapshot_fn=snapshot_fn or _created_snapshot)
    if snapshot is None:
        return SessionObservation(
            False,
            None,
            "no tmux probe could see this host's sessions (a wedged server, "
            "or we are inside a container whose tmux is a different mount "
            "namespace) — that is a non-observation, not an empty fleet",
        )
    created = snapshot.get(session)
    if created is None:
        return SessionObservation(True, None, "")
    return SessionObservation(True, f"{session}@{created}", "")


def verify_cycled(
    name: str,
    before: str | None,
    after: str | None,
    *,
    session_before: SessionObservation | None = None,
    session_after: SessionObservation | None = None,
) -> RestartVerdict:
    """Decide whether ``name`` actually cycled between ``before`` and ``after``.

    See the module docstring for the ternary contract and for why the
    ledger alone can never reach ``True``. The cases are written out one
    per branch, in evidence order, so the decision reads as a table rather
    than as a chain of conditions.

    ``session_before`` / ``session_after`` are the OS-side witness
    (:func:`read_session_identity`). Omitting them is not a shortcut to a
    pass: the default is the BLIND observation, so a caller that offers no
    process evidence gets ``None`` ("cannot verify"), never ``True``.
    """
    seen_before = session_before or SessionObservation()
    seen_after = session_after or SessionObservation()

    if before is not None and after == before:
        return RestartVerdict(
            False,
            f"agent {name!r} did NOT cycle: it is still run {before} — the "
            f"restart changed nothing, so it is the OLD process on its OLD "
            f"credentials",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )
    if before is not None and after is None:
        return RestartVerdict(
            False,
            f"agent {name!r} has NO run after the restart (was {before}) — "
            f"the stop leg ran but nothing came back up",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )
    if after is None:
        return RestartVerdict(
            None,
            f"agent {name!r} had no instance_id marker before OR after, so the "
            f"restart could not be verified either way (no evidence — this is "
            f"NOT a reported failure)",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )

    # The ledger says NEW RUN. On its own that is an echo of the write the
    # start path just made, so the OS has to corroborate it before this
    # reports a pass.
    if not (seen_before.observed and seen_after.observed):
        blind = seen_after.blind_because or seen_before.blind_because
        return RestartVerdict(
            None,
            f"agent {name!r} has a NEW run id ({before or 'no run'} -> "
            f"{after}), but that id was minted by the very start path we are "
            f"checking — and NO process observation backs it up: {blind}. "
            f"CANNOT VERIFY (not a reported failure); confirm by hand with "
            f"`tmux ls`",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )
    if seen_after.identity is None:
        return RestartVerdict(
            False,
            f"agent {name!r} has a NEW run id ({before or 'no run'} -> "
            f"{after}) but NO session is running for it — the ledger says it "
            f"came back up and the OS says it did not",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )
    if seen_after.identity == seen_before.identity:
        return RestartVerdict(
            False,
            f"agent {name!r} did NOT cycle: the ledger minted a new run id "
            f"({before or 'no run'} -> {after}) but its session "
            f"{seen_after.identity} is the SAME one, at the same birthday — "
            f"the restart wrote rows and never touched the process. Kill it "
            f"by hand: tmux kill-session -t "
            f"{seen_after.identity.rsplit('@', 1)[0]}",
            before,
            after,
            seen_before.identity,
            seen_after.identity,
        )
    return RestartVerdict(
        True,
        f"agent {name!r} is a NEW run ({before or 'no run'} -> {after}) and "
        f"the OS agrees: its session cycled "
        f"({seen_before.identity or 'no session'} -> {seen_after.identity})",
        before,
        after,
        seen_before.identity,
        seen_after.identity,
    )
