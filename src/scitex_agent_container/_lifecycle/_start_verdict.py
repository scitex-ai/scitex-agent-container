"""The already-running decision for ``agent_start`` — a VERDICT, not a bool.

WHAT THIS REPLACES
------------------
``agent_start`` used to gate its no-op branch on::

    really_running = (registry.exists(name)
                      and runtime.is_running(config)
                      and _verify_real_liveness(config, runtime))

Three proxies, AND-ed into one bit, then acted on. Both directions bit us:

* **false GREEN.** Every one of those signals is PID/row-shaped. A wedged,
  auth-dead or deaf agent whose process still exists satisfies all three, so
  ``sac agents start`` printed *"already running. No-op."* at a corpse —
  forever. The row was UNFALSIFIABLE: nothing in the gate could ever come back
  and say "actually, no". The only escape was ``--force --fresh``, which
  DESTROYS the session. So the remedy for *"I am not sure this is alive"* was
  *"kill it"*, which is precisely backwards, and a no-op you can only escape by
  destroying something is not safety — it is a trap.

* **false RED.** Symmetrically, a probe that could not RUN returned ``False``,
  which is indistinguishable from "probed, and confirmed absent". A wedged
  tmux, or a prober that cannot see the tmux socket at all, marks the whole
  fleet dead at once.

WHAT IT DOES NOW
----------------
The same signals are gathered as EVIDENCE and folded into a ternary verdict
(:mod:`._verdict`). Only an :data:`._verdict.ALIVE` verdict — positive evidence
that *something observed this agent alive* — pins the no-op.

**UNKNOWN NO LONGER NO-OPS.** An agent nothing can vouch for is now startable
without ``--force --fresh``.

WHY THAT IS SAFE (the part that has to be defended)
---------------------------------------------------
Because **starting is not destroying**, and the runtimes already carry their
own duplicate guard as the backstop::

    # runtimes/tui_session.py :: TuiSessionRuntime.start
    if self._mux.exists(name) and not force:
        system_msg(f"duplicate session '{name}' — agent already running. ...")
        if not dry_run:
            return True          # <- no relaunch, no kill, no data loss

So if an UNKNOWN agent turns out to have been alive all along, the real start
reaches that guard and no-ops there instead of relaunching over a live session.
We keep the safety property and drop the trap. The dangerous verb is ``--force``
(which STOPS first) — and that stays exactly where it was: behind an explicit
operator flag.

Note this makes ``agent_start`` *less* destructive than before, not more. The
old gate fell through to the full start path — hooks,
``kill_orphan_mcp_children`` (which SIGKILLs processes matched by agent name),
workspace re-materialisation — whenever the ``instances`` row happened to be
missing, which on this fleet is routine for healthy agents. Now a live agent is
recognised as ALIVE and we stop before any of that runs.
"""

from __future__ import annotations

from typing import Any, Callable

from ._verdict import (
    ALIVE,
    INSTRUMENT_PID_NAMESPACE,
    SOURCE_REGISTRY,
    UNKNOWN,
    LivenessVerdict,
    Signal,
    decide,
)

__all__ = ["resolve_start_verdict"]


def resolve_start_verdict(
    config: Any,
    runtime: Any,
    *,
    registry: Any,
    liveness_verifier: Callable[[Any, Any], bool] | None = None,
    resolver: Callable[..., LivenessVerdict] | None = None,
) -> LivenessVerdict:
    """Resolve the ALIVE / DEAD / UNKNOWN verdict that gates the no-op branch.

    ``liveness_verifier`` is the pre-existing injection seam
    (``Callable[[AgentConfig, runtime], bool]``). It is honoured with its
    ORIGINAL semantics, mapped faithfully onto the verdict:

    * all three legacy signals true (registry row + ``runtime.is_running`` +
      the verifier) → :data:`._verdict.ALIVE`, so the caller no-ops exactly as
      it always did;
    * anything else → :data:`._verdict.UNKNOWN`, so the caller proceeds to a
      real start exactly as it always did.

    A bool cannot express death, so an injected verifier can never produce a
    ``DEAD`` — and it does not need to: ``UNKNOWN`` and ``DEAD`` lead to the
    same (non-destructive) place here, a real start. Behaviour for every
    existing caller of the seam is therefore bit-for-bit unchanged.

    Without an injected verifier we take the real, evidence-gathering path
    (:func:`._verdict_resolve.resolve_verdict`), which asks the ONE
    authoritative thing — the listen broker, i.e. can a message actually reach
    this agent — alongside the process / heartbeat / registry hints.
    """
    if liveness_verifier is not None:
        legacy_alive = bool(
            registry.exists(config.name)
            and runtime.is_running(config)
            and liveness_verifier(config, runtime)
        )
        detail = (
            "injected liveness_verifier + registry row + runtime.is_running all "
            "agree the agent is up"
            if legacy_alive
            else "injected liveness_verifier (or the registry row / "
            "runtime.is_running) did not vouch for this agent — no positive "
            "evidence of life, which is not the same as evidence of death"
        )
        # INSTRUMENT: this legacy signal is a CONJUNCTION spanning the registry
        # row, ``runtime.is_running`` and the injected verifier — all of them
        # pid/row-shaped. It can only ever emit ALIVE or UNKNOWN (a bool cannot
        # express death), so it can never convict; and per the AMBIGUITY RULE we
        # label the instrument that COLLAPSES with an existing one rather than
        # inventing an independent witness.
        return decide(
            config.name,
            [
                Signal(
                    SOURCE_REGISTRY,
                    ALIVE if legacy_alive else UNKNOWN,
                    detail,
                    INSTRUMENT_PID_NAMESPACE,
                )
            ],
        )

    if resolver is None:
        from ._verdict_resolve import resolve_verdict as resolver  # type: ignore

    return resolver(config.name, config, runtime)
