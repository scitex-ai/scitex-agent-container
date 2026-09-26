"""The rate-wall reviver: the THIRD agent-liveness shape, and the one nothing owned.

sac's two existing enforcers are defined by each other, and a rate wall falls
between them:

    no tmux session               a CORPSE     ``sac.fleet-reconcile``
    session + frozen auth banner  a WEDGE      ``sac.restart-login-expired-agents``
    session + frozen rate banner  a PAUSE      here

INCIDENT 2026-08-28. A session limit stopped a set of agents at ~17:25 UTC;
the limit lifted at 19:10 UTC; nothing resumed until the operator asked at
20:56 UTC. One hour forty-six minutes of dead time a human had to catch.

Neither existing enforcer was at fault, which is what made the hole invisible.
``fleet-reconcile`` saw live tmux sessions and correctly handed off — measured
on scitex-compute-04, sessions created 05:33 UTC were still listed at 21:15
UTC, so its rule returned ``OK``/``session-alive``. The auth healer's matcher
excludes 429 by design and says why at the exclusion: *a restart does not fix
a rate wall*. It is right, and the corollary nobody had written is that a
rate wall does not need fixing — it needs WAITING OUT and then CONTINUING.

Layout — the rule is separated from the doing, as in the siblings:

* :mod:`._banner` — the PURE pane parser: is there a wall, and when does it
  lift? Fitted to real captured banners, and it never guesses a reset.
* :mod:`._rule`   — the PURE decision table (no IO, no clock of its own).
* :mod:`._resume` — the one mutation: a VERIFIED nudge, never a restart.
* :mod:`._alarm`  — the rails, and an honest note about who reads them.
* :mod:`._pass`   — the IO: read panes, hold what is walled, wake what is free.

Driven by ``sac agents resume-rate-limited`` (read-only by default) and
scheduled as the ``sac.resume-rate-limited-agents`` JobSpec.

THE FOURTH SHAPE — a wall that WAITING cannot end (2026-09-06)
    A rate wall publishes an end. A MODEL cap does not. Measured tonight: the
    operator sent two messages to a Fable-family agent and both were answered
    by the harness with ``You've reached your Fable limit. Run /usage-credits
    to continue or switch models with /model.`` — no reset clause anywhere, so
    the machinery above has nothing to wait for and correctly reports
    ``NOT-LIMITED`` while the agent stays silent to the operator. Three
    workflow subagents died in the same window behind ``You've hit your
    session limit · resets 2am (UTC)``, which does publish an end — hours
    away.

    Both are walls a MODEL SWITCH ends in seconds, so the same four-part
    separation is repeated for that remedy: :mod:`._modelcap` (the pure
    parser), :mod:`._switch_rule` (the pure rule), :mod:`._switch` (the one
    mutation — the operator's three steps, three seconds apart, then a
    verification) and :mod:`._switch_pass` (its own ledger and perform leg).
    It is OFF by default and armed with ``--switch-model``.

WHY THIS CANNOT HOT-LOOP AGAINST A LIVE LIMIT
    Structurally, not by tuning. The resume branch of :func:`._rule.decide`
    is unreachable until ``now >= reset_at``, where ``reset_at`` is read from
    the provider's own banner. A wall we cannot time yields ``RESET_UNKNOWN``
    and is HELD, not guessed at. So the pass cannot spend a token against a
    limit that is still standing, which is the failure mode that would make
    an outage longer instead of shorter.
"""

from __future__ import annotations

from ._alarm import SUBSYSTEM
from ._banner import LimitObservation, observe_pane
from ._modelcap import ModelCapObservation, observe_model_cap, verify_switch
from ._pass import (
    DEFAULT_INTERVAL,
    DEFAULT_PASS_CAP,
    AgentReport,
    PassOutcome,
    history_path,
    resume_pass,
)
from ._resume import RESUME_MESSAGE
from ._rule import MANAGED_POLICIES, Decision, Verdict, decide
from ._switch import KICK_MESSAGE, SWITCH_STEP_DELAY_S, switch_model_now
from ._switch_pass import switch_history_path
from ._switch_rule import TARGET_MODEL, decide_switch

__all__ = [
    "AgentReport",
    "DEFAULT_INTERVAL",
    "DEFAULT_PASS_CAP",
    "Decision",
    "KICK_MESSAGE",
    "LimitObservation",
    "MANAGED_POLICIES",
    "ModelCapObservation",
    "PassOutcome",
    "RESUME_MESSAGE",
    "SUBSYSTEM",
    "SWITCH_STEP_DELAY_S",
    "TARGET_MODEL",
    "Verdict",
    "decide",
    "decide_switch",
    "history_path",
    "observe_model_cap",
    "observe_pane",
    "resume_pass",
    "switch_history_path",
    "switch_model_now",
    "verify_switch",
]
