"""The PURE rule: is this agent a CORPSE that sac promised to restart?

No IO lives here — every input is passed in, so the decision table below is
exercised directly by tests instead of being inferred from a live fleet.

Why this module exists at all
-----------------------------
``restart: {policy: on-failure, max_retries: N}`` appears in ~93 specs and
is DEAD CODE. :mod:`.._lifecycle._start` launches the loop that reads it
(:func:`.._lifecycle.health.health_monitor`) as ``thread_factory(...,
daemon=True)`` and then RETURNS — but ``sac agents start`` is a short-lived
CLI, and a daemon thread dies with its parent. The file says as much about
its sibling poller: "Daemon thread, dies with the process." The resident
``sac listen`` daemon only reconciles CARDS
(:mod:`.._listen._liveness_tick`: "sac only DETECTS and EMITS"). So nothing
in sac ever enforced "should be running ⇒ is running", and when an OAuth
rotation killed 33 agents they stayed dead until the operator noticed by
chance. This is the missing enforcer's decision table.

THE TWO PRINCIPLES THIS TABLE ENCODES
-------------------------------------
1. **Only ever restart a CORPSE.** A dead tmux session holds no context, so
   restarting it cannot destroy anything — that is what makes this the rare
   safe auto-remedy. A live-but-wedged agent is NOT ours (``auth-heal.py``
   owns that), and a deliberately-stopped one is the operator's own
   decision, which is sacred.
2. **"I could not look" is not "nothing is there".** The tmux read is
   namespace-scoped: from inside a SIF ``tmux ls`` succeeds and reports an
   EMPTY fleet (see :mod:`.._lifecycle._verdict_tmux`, which measured a
   false DEAD verdict on a live agent this way). A binary
   present/absent would therefore restart the ENTIRE fleet the first time
   this ran somewhere blind. ``probe_ran is None`` ⇒ :attr:`Verdict.UNKNOWN`,
   never a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "DELIBERATE_EXIT_REASONS",
    "Decision",
    "MANAGED_POLICIES",
    "UNEXPECTED_EXIT_REASONS",
    "Verdict",
    "decide",
]

#: ``restart.policy`` values that put an agent under this enforcer's care.
#: ``RestartSpec.policy`` DEFAULTS to ``"never"``, so a spec that merely
#: omits a ``restart:`` block is never touched — the opt-in is explicit.
MANAGED_POLICIES = ("always", "on-failure")

#: ``exit_reason`` values that mean a DELIBERATE end: ``sac agents stop``
#: writes ``stopped``, ``sac agents delete`` writes ``deleted``. The
#: operator's intent — never second-guess it, never "helpfully" undo it.
DELIBERATE_EXIT_REASONS = ("stopped", "deleted")

#: ``exit_reason`` values that mean it DIED without being asked to: the
#: reaper writes ``crashed``, the host-reboot sweep writes ``reboot-swept``.
UNEXPECTED_EXIT_REASONS = ("crashed", "reboot-swept")

#: ``exit_reason`` values that are sac's own internal bookkeeping rather
#: than a statement about the agent's fate. Not a corpse to resurrect: a
#: ``superseded`` row means a NEWER row took over, and ``stale-cleared``
#: means the row was tidied, not that the agent crashed.
_BOOKKEEPING_EXIT_REASONS = ("superseded", "stale-cleared")


class Verdict(str, Enum):
    """What we concluded about ONE agent. Every leg is printed, never silent.

    The RULE in this module only ever returns the first five. The remaining
    verdicts are reachable only from a PASS: most describe what it then DID
    with a :attr:`RESTART`, while :attr:`UNOBSERVED` records the agents it
    never managed to take a reading of at all.
    """

    # --- reachable by the pure rule ---------------------------------------
    NOT_MANAGED = "NOT-MANAGED"  # restart.policy is never/absent → not ours
    OK = "OK"  # a tmux session exists → alive → hands off
    UNKNOWN = "UNKNOWN"  # we could not look. NOT "nothing is there".
    SKIPPED = "SKIPPED"  # a corpse we must NOT resurrect (see `reason`)
    RESTART = "RESTART"  # a corpse sac promised to bring back

    # --- reachable only by the pass (what it did with a RESTART) ----------
    WOULD_RESTART = "WOULD-RESTART"  # --dry-run: reported, not performed
    RESTARTED = "RESTARTED"
    FAILED = "FAILED"  # we tried and it did not come back
    OVER_BUDGET = "OVER-BUDGET"  # out of hourly budget → CARDED, not restarted
    COOLING_DOWN = "COOLING-DOWN"  # inside the debounce → wait, do not card
    CAPPED = "CAPPED"  # this pass's global cap spent; next pass retries
    BUDGET_UNKNOWN = "BUDGET-UNKNOWN"  # we cannot read our OWN memory → refuse

    # Not a thing a pass DID — a thing it could not do. An agent whose pane
    # would not capture, or that is registered with no live session at all,
    # produced no evidence, and no evidence is neither OK nor AUTH-FAILED. It
    # is therefore never restarted and never counted clean; it is REPORTED, so
    # that the one agent nobody could read is the one agent nobody can miss.
    #
    # Distinct from :attr:`UNKNOWN` above, which is the pure rule's fleet-wide
    # "the tmux probe was not a sensor from where we stand". This one is
    # per-agent and per-reading: the probe worked, this agent did not answer.
    UNOBSERVED = "UNOBSERVED"


@dataclass(frozen=True)
class Decision:
    """A verdict plus WHY, in both machine and human form.

    ``reason`` is a short stable code (tests + JSON key off it); ``detail``
    is the full sentence printed to the operator. Both are mandatory: a
    verdict whose cause is not stated is exactly the silent skip this whole
    command exists to abolish.
    """

    verdict: Verdict
    reason: str
    detail: str


def decide(
    *,
    name: str,
    policy: str,
    probe_ran: bool | None,
    session_present: bool | None,
    row: Mapping[str, Any] | None,
    local_host: str | None = None,
) -> Decision:
    """Decide ONE agent's fate from facts alone. Pure — no IO, no clock.

    Parameters
    ----------
    policy
        ``spec.restart.policy``.
    probe_ran, session_present
        Straight from
        :func:`.._lifecycle._verdict_tmux.tmux_session_observation`.
        ``probe_ran is None`` means the tmux read was not a sensor from
        where we stand, and ``session_present`` is then meaningless.
    row
        The latest ``instances`` row for ``name``
        (:func:`.._state.state_db_instances.last_known_instance`), or
        ``None`` when the agent has never appeared in this host's registry.
    local_host
        This machine's name as ``instances.host`` records it. When given, a
        row belonging to ANOTHER host is skipped — see the remote guard.
    """
    if policy not in MANAGED_POLICIES:
        return Decision(
            Verdict.NOT_MANAGED,
            "policy-never",
            f"restart.policy={policy!r} — sac never promised to keep {name} "
            f"running, so nothing to enforce",
        )

    # --- 2. TMUX IS THE FACT, and a non-observation is not a fact ---------
    if probe_ran is None:
        return Decision(
            Verdict.UNKNOWN,
            "could-not-look",
            f"could NOT read the host's tmux for {name} (blind from here — a "
            f"container's tmux is a different namespace, and an 'empty fleet' "
            f"seen from inside one is blindness, not an empty fleet). Refusing "
            f"to infer death from a non-observation",
        )
    if session_present:
        return Decision(
            Verdict.OK,
            "session-alive",
            f"tmux session for {name} exists — alive; hands off (a wedged "
            f"session is auth-heal's job, never ours)",
        )

    # --- No session, and we genuinely looked. Is it a corpse WE own? ------
    if row is None:
        # A spec with no instances row was never started HERE. Bringing it
        # up would be a START, not a re-start: nobody asked for it, and
        # doing so for every unstarted spec on the host at once is a fleet
        # storm. The enforcer restores agents that DIED, not agents that
        # never ran.
        return Decision(
            Verdict.SKIPPED,
            "never-started",
            f"no tmux session and NO instances row — {name} has never started "
            f"on this host, so there is no corpse to resurrect (starting it "
            f"would be a start nobody asked for, not a restart)",
        )

    if row.get("remote"):
        return Decision(
            Verdict.SKIPPED,
            "remote",
            f"{name}'s row is remote=1 (it landed on another host) — this "
            f"pass reads only the LOCAL tmux, so its absence here is not "
            f"evidence of death, and restarting it locally would DUPLICATE a "
            f"live remote agent",
        )

    row_host = row.get("host")
    if local_host and row_host and row_host != local_host:
        return Decision(
            Verdict.SKIPPED,
            "other-host",
            f"{name}'s row was written on host {row_host!r} but we are on "
            f"{local_host!r} — its tmux is not ours to read, so its absence "
            f"here is not evidence of death",
        )

    # ORDER IS LOAD-BEARING: the ghost-active row is checked BEFORE
    # exit_reason. A row still claiming to be active while its session is
    # gone is the corpse signature — exactly the state 33 agents were left
    # in when the OAuth rotation killed them. `record_instance_stop` only
    # ever writes ended_at and exit_reason TOGETHER, so a live ended_at
    # with a deliberate exit_reason cannot occur; if it somehow did, the
    # missing ended_at is the stronger evidence that nobody ended this.
    if row.get("ended_at") is None:
        return Decision(
            Verdict.RESTART,
            "ghost-active-row",
            f"no tmux session but {name}'s instances row still claims ACTIVE "
            f"(ended_at IS NULL) — nothing recorded an end, so it DIED "
            f"unexpectedly. This is the corpse signature",
        )

    exit_reason = row.get("exit_reason")
    if exit_reason in DELIBERATE_EXIT_REASONS:
        return Decision(
            Verdict.SKIPPED,
            "deliberate",
            f"{name} was DELIBERATELY ended (exit_reason={exit_reason!r}) — "
            f"that is the operator's decision and it is sacred; sac will not "
            f"undo it",
        )
    if exit_reason in UNEXPECTED_EXIT_REASONS:
        return Decision(
            Verdict.RESTART,
            "died-unexpectedly",
            f"no tmux session and {name} ended with exit_reason="
            f"{exit_reason!r} — it died without being asked to",
        )
    if exit_reason in _BOOKKEEPING_EXIT_REASONS:
        return Decision(
            Verdict.SKIPPED,
            "bookkeeping",
            f"{name}'s row ended with exit_reason={exit_reason!r} — sac's own "
            f"internal bookkeeping, not a statement that the agent died",
        )
    return Decision(
        Verdict.SKIPPED,
        "unrecognised-exit-reason",
        f"{name}'s row ended with an exit_reason this rule does not know "
        f"({exit_reason!r}) — refusing to guess that it means death. If this "
        f"is a real crash reason, add it to UNEXPECTED_EXIT_REASONS",
    )
