"""The model-switch branch of the resume pass: its memory, and its one perform.

Split out of :mod:`._pass` rather than grown into it. The pass is already the
IO half of a three-part enforcer (pure parser / pure rule / one mutation), and
adding a SECOND remedy's ledger and perform step inline would have made the
one file that everybody reads the one file nobody can hold in their head.
:mod:`._pass` keeps the sweep; this keeps everything that is specific to
switching a capped agent's model.

THE LEDGER IS THIS REMEDY'S OWN, and that is not tidiness
----------------------------------------------------------
``SAC_MODEL_SWITCH_HISTORY`` is separate from ``SAC_RATE_LIMIT_HISTORY``,
which is itself separate from ``SAC_RECONCILE_HISTORY``. The ledger format is
a flat ``{agent: [epoch, ...]}`` with no remedy key, so two remedies sharing a
file would silently consume each other's budget: one model switch would then
disarm the resume debounce for the same agent, and a pass that resumed an
agent would look, to this branch, like a pass that had already switched it.

The numbers are inherited from :mod:`.._reconcile._budget` unchanged — a
30-minute per-agent debounce, at most 2 per agent per rolling hour, and a
per-pass cap. They were chosen so a persistently-failing remedy becomes a
REPORT instead of a loop, and that argument is remedy-independent.

WHY A THREE-VALUED RESULT GETS THREE VERDICTS
---------------------------------------------
:func:`._switch.switch_model_now` answers ``True`` / ``False`` / ``None``, and
all three land here as distinct verdicts:

* ``SWITCHED``          — proven: the cap is gone and something other than our
  own keystrokes says so.
* ``SWITCH_FAILED``     — proven otherwise: the cap banner is still rendered
  after all three steps.
* ``SWITCH_UNVERIFIED`` — we typed everything and cannot show it took. Exit 2,
  because "I could not tell" must never be logged as a healthy tick.

All three SPEND budget. A switch we could not verify cost the agent a real
turn and possibly changed its model, so retrying it on the next 5-minute tick
without a debounce is exactly the hot loop the budget exists to prevent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .._reconcile._budget import Budget
from ._rule import Decision, Verdict

__all__ = [
    "SWITCH_SPENT",
    "already_switched",
    "record_switch",
    "switch_history_path",
    "switch_report",
]

#: Explicit override for WHERE this remedy's memory lives — SEPARATE from both
#: siblings'. See the module docstring for why sharing one file would make two
#: remedies eat each other's budget.
_HISTORY_ENV = "SAC_MODEL_SWITCH_HISTORY"

#: Verdicts that SPEND budget: we typed into the agent's pane, whether or not
#: we could prove the outcome. An unverifiable switch must cost the same as a
#: proven one, or an agent we cannot read becomes one we retype into forever.
SWITCH_SPENT = (Verdict.SWITCHED, Verdict.SWITCH_FAILED, Verdict.SWITCH_UNVERIFIED)

_BUDGET_VERDICTS = {
    "debounce": Verdict.COOLING_DOWN,
    "over-budget": Verdict.OVER_BUDGET,
    "pass-cap": Verdict.CAPPED,
}


def switch_history_path() -> Path:
    """Where this remedy remembers whose model it already switched.

    Resolved PER CALL, never cached at import: a module-level constant would
    be baked from an env var that tests set afterwards, which is the exact
    trap :mod:`.._state.state_paths` documents having paid for.
    """
    override = os.environ.get(_HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "model-switch-history.json"


def already_switched(budget: Budget | None, name: str, now: float) -> bool:
    """Have we ALREADY switched this agent so recently that we cannot see it?

    The pure rule's no-second-fire input. It reads only the DEBOUNCE leg of
    the budget, deliberately: over-budget and pass-cap are refusals about
    volume and are the perform step's to make, while the debounce is the one
    that answers the rule's actual question — *has this agent had time to show
    us what the last switch did?*

    A ``None`` budget answers ``False``, which sounds wrong and is not: an
    unreadable ledger must not become a silent "yes, already done" that hides
    a capped agent. The refusal to act on an unenforceable budget belongs to
    :func:`switch_report`, which states it as ``BUDGET-UNKNOWN`` instead of
    swallowing it here.
    """
    if budget is None:
        return False
    return budget.check(name, now).reason == "debounce"


def record_switch(
    history_file: Path,
    budget: Budget,
    *,
    name: str,
    now: float,
    stream: Any,
) -> None:
    """Spend this agent's switch budget and persist it. Never raises.

    A ledger write that fails must stop this pass from switching anything
    ELSE — the alternative is an unbounded loop with no memory — but must not
    lose the reports already earned, so the pass is capped in place rather
    than aborted.
    """
    from .._reconcile._budget import save_history

    budget.record(name, now)
    # stx-allow: fallback (reason: an unwritable ledger must cap this pass rather than abort it — the reports already earned still reach the `sac agents resume-rate-limited` stdout line, the pass-record counts in runtime_root()/sac-events.jsonl, and the exit code the supervisor persists)
    try:
        save_history(history_file, budget.history, now=now)
    except OSError as exc:
        budget.spent = budget.pass_cap
        print(
            f"[rate-limit-resume] CANNOT RECORD model switches to "
            f"{history_file}: {exc} — capping this pass so no other agent's "
            f"model is changed without memory",
            file=stream,
        )


def switch_report(
    name: str,
    decision: Any,
    *,
    budget: Budget | None,
    apply: bool,
    now: float,
    switch_fn: Callable[[str, str], bool | None],
) -> Decision:
    """Turn ONE authorised ``SWITCH-MODEL`` into what actually happened to it.

    Mirrors :func:`._pass._perform` leg for leg, so the two remedies refuse in
    the same order and for the same reasons, and returns a
    :class:`._rule.Decision` so the pass's existing reporting path carries it
    without a second report type.
    """
    if budget is None:
        return Decision(
            Verdict.BUDGET_UNKNOWN,
            "switch-budget-unreadable",
            f"{name} is capped on a switchable model, but this pass cannot "
            f"read its own model-switch ledger, so it cannot honour the "
            f"debounce. Refusing to retype /model into a pane on a budget it "
            f"cannot enforce",
        )
    check = budget.check(name, now)
    if not check.allowed:
        return Decision(
            _BUDGET_VERDICTS[check.reason],
            check.reason,
            f"{decision.detail} — but {check.detail}",
        )
    if not apply:
        return Decision(Verdict.WOULD_SWITCH, decision.reason, decision.detail)

    switched = switch_fn(name, decision.target)
    if switched is True:
        return Decision(
            Verdict.SWITCHED,
            decision.reason,
            f"{decision.detail} — switched to {decision.target!r} and the "
            f"pane PROVES it: the cap banner is gone",
        )
    if switched is False:
        return Decision(
            Verdict.SWITCH_FAILED,
            decision.reason,
            f"{decision.detail} — all three steps were typed and the cap "
            f"banner is STILL rendered, so this agent is still silent. "
            f"Recorded as degraded rather than retyped",
        )
    return Decision(
        Verdict.SWITCH_UNVERIFIED,
        decision.reason,
        f"{decision.detail} — all three steps were typed and the pane does "
        f"not prove the switch either way. Reported as an ambiguity, never "
        f"as a recovery: a switcher that claims success it cannot show hands "
        f"the operator a fleet he believes is working",
    )
