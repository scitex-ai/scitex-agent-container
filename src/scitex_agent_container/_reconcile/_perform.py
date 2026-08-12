"""Turning authorised restarts into what actually happened.

Extracted from :mod:`._pass` (at the per-file cap) and cohesive on its own
terms: everything here is about SPENDING — budget, the restart itself, and
persisting the record of it before the next agent is touched.

It also holds the fleet-wide gate. A corpse is only interpretable next to its
neighbours, so :func:`resolve_pending` takes the whole pass's RESTART
authorisations at once, asks :mod:`._blackout` whether they are N deaths or one
event, and only then acts. That ordering is the point: a per-agent loop that
restarts as it goes cannot see that it is the tenth corpse in a row.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from ._blackout import blackout_detail, is_fleet_blackout
from ._budget import Budget, save_history
from ._report import AgentReport
from ._rule import Verdict  # noqa: F401  (re-exported for _pass's callers)

__all__ = ["_BUDGET_VERDICTS", "_SPENT", "perform", "resolve_pending"]

#: Verdicts that mean we ATTEMPTED a restart, so the history must be
#: persisted before we touch the next agent. FAILED counts: a restart that
#: raised still consumed a real attempt, and forgetting it would let the
#: next tick retry immediately.
_SPENT = (Verdict.RESTARTED, Verdict.FAILED)

#: Which rate-limit stood in the way — and, crucially, whether it means
#: "wait" or "a human must look". Only :attr:`Verdict.OVER_BUDGET` is
#: carded (see :mod:`._alarm`), and the difference is not cosmetic:
#:
#: * ``debounce`` (COOLING-DOWN) is the NORMAL state of a healthy recovery.
#:   The debounce is 30min and the timer ticks every 5, so a perfectly good
#:   restart is inside its own debounce for the next five ticks. Carding
#:   that would mint a board card for every successful heal — training the
#:   operator to ignore the board, which is how the fleet died unnoticed in
#:   the first place.
#: * ``over-budget`` means we have already bounced it twice in an hour AND
#:   waited out two debounces, and it is STILL down. Restarting is not
#:   fixing this: that is a real, human-shaped problem, so it is carded.
#: * ``pass-cap`` (CAPPED) is our own throttle, not the agent's fault. The
#:   next tick picks it up 5 minutes later.
_BUDGET_VERDICTS = {
    "debounce": Verdict.COOLING_DOWN,
    "over-budget": Verdict.OVER_BUDGET,
    "pass-cap": Verdict.CAPPED,
}


def perform(
    name: str,
    decision,
    *,
    budget: Budget | None,
    apply: bool,
    now: float,
    restart_fn: Callable[[str], bool],
    budget_detail: str = "",
) -> AgentReport:
    """Turn the rule's RESTART authorisation into what we actually did."""
    if budget is None:
        # We could not read our OWN restart memory, so the debounce and the
        # hourly cap cannot be enforced. Restarting anyway would not be
        # "trying harder" — with no memory, EVERY corpse is restartable on
        # EVERY 5-minute tick, forever. That is the restart loop the limits
        # exist to prevent, and it is strictly worse than a down agent.
        return AgentReport(
            name,
            Verdict.BUDGET_UNKNOWN,
            "budget-unreadable",
            f"{decision.detail}; NOT restarted: {budget_detail}",
        )
    check = budget.check(name, now)
    if not check.allowed:
        return AgentReport(
            name,
            _BUDGET_VERDICTS[check.reason],
            check.reason,
            f"{decision.detail}; NOT restarted: {check.detail}",
        )

    if not apply:
        return AgentReport(
            name,
            Verdict.WOULD_RESTART,
            decision.reason,
            f"{decision.detail} — would restart (dry-run: nothing was done; "
            f"re-run with --apply to actually restart)",
        )

    # stx-allow: fallback (reason: one agent's restart raising must never abort the sweep — the rest of the fleet is still down and still needs recovering; the failure is carded and reported)
    try:
        ok = restart_fn(name)
    except Exception as exc:
        budget.record(name, now)  # a restart we ATTEMPTED still spends budget
        return AgentReport(
            name,
            Verdict.FAILED,
            "restart-raised",
            f"{decision.detail}; restart FAILED: {exc}",
        )
    budget.record(name, now)
    if ok:
        return AgentReport(name, Verdict.RESTARTED, decision.reason, decision.detail)
    return AgentReport(
        name,
        Verdict.FAILED,
        "restart-returned-false",
        f"{decision.detail}; restart ran but reported FAILURE — the agent is "
        f"still down",
    )


def resolve_pending(
    pending: list[tuple[str, Any, str]],
    *,
    server_present: bool | None,
    budget: Budget | None,
    apply: bool,
    now: float,
    restart_fn: Callable[[str], bool],
    budget_detail: str,
    history_file: Path,
    stream: Any = None,
) -> list[AgentReport]:
    """Act on every RESTART this pass authorised — or refuse them together.

    The fleet-wide gate runs FIRST and applies to the whole batch, because the
    evidence it reads is a property of the pass rather than of any one agent:
    multiple corpses AND no live session anywhere. See :mod:`._blackout`.

    A blackout refuses in the SAFE direction — it withholds restarts and spends
    no budget, so the next pass can still recover the fleet the moment a live
    session reappears. Nothing here marks an agent dead.
    """
    stream = stream if stream is not None else sys.stderr
    names = tuple(name for name, _, _ in pending)

    if is_fleet_blackout(
        server_present=server_present, restart_count=len(pending)
    ):
        detail = blackout_detail(len(pending), names)
        print(f"[fleet-reconcile] {detail}", file=stream)
        return [
            AgentReport(
                name,
                Verdict.FLEET_BLACKOUT,
                "fleet-blackout",
                f"{decision.detail}; NOT restarted: {detail}",
                policy,
            )
            for name, decision, policy in pending
        ]

    reports: list[AgentReport] = []
    for name, decision, _policy in pending:
        report = perform(
            name,
            decision,
            budget=budget,
            apply=apply,
            now=now,
            restart_fn=restart_fn,
            budget_detail=budget_detail,
        )
        reports.append(report)
        # PERSIST THE MOMENT WE SPEND BUDGET, never only at the end. The
        # scheduled form runs under a systemd timeout, and a pass killed
        # mid-sweep with its history still in RAM would forget the agents it
        # had just bounced — so the next tick would bounce them again,
        # debounce and hourly cap silently disarmed. That is the restart
        # LOOP these limits exist to prevent, re-introduced by the very
        # timeout meant to contain the pass. One small atomic write per
        # restart (<=`limit` per pass) buys immunity to it.
        if apply and budget is not None and report.verdict in _SPENT:
            # stx-allow: fallback (reason: if we can no longer RECORD restarts we must stop PERFORMING them — a restart we cannot remember is an unbounded one. Spending the pass cap halts further restarts safely; the loud print + non-zero exit carry the failure.)
            try:
                save_history(history_file, budget.history, now=now)
            except OSError as exc:
                budget.spent = budget.pass_cap  # authorise no further restarts
                print(
                    f"[fleet-reconcile] CANNOT RECORD restarts to "
                    f"{history_file} ({exc}) — halting this pass's restarts. "
                    f"An unrecordable restart is an unbounded one.",
                    file=stream,
                )
    return reports
