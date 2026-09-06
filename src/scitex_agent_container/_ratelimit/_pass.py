"""One resume pass: read every live pane, hold what is walled, wake what is free.

The IO half. The decision is pure and lives in :mod:`._rule`; the banner
parse in :mod:`._banner`; the one mutation in :mod:`._resume`. This module
only wires facts into the rule and carries out what the rule authorises.

Every collaborator is an injectable seam with a REAL default, so tests drive
the whole pass against real temp files, real captured panes and a recorder in
place of the single irreversible act. No mocks.

BLINDNESS HERE CAN ONLY CAUSE INACTION
--------------------------------------
This pass acts only on an agent it can SEE a frozen banner on, so every way
of failing to look — no tmux server, an unreadable pane, an empty session
list — removes agents from the candidate set rather than adding them. That is
the opposite of ``sac.fleet-reconcile``, where a blind read once meant "the
whole fleet is dead" and could have restarted all of it; that job needs a
blackout breaker, and this one structurally cannot want one. Blindness is
still REPORTED (``UNREADABLE``, exit 2) — inaction we did not choose is not
the same as a clean pass.

THE BUDGET IS THIS ENFORCER'S OWN
---------------------------------
It keeps its own history file (``SAC_RATE_LIMIT_HISTORY``), exactly as
``restart-login-expired`` keeps one separate from ``fleet-reconcile``'s: the
ledger is a flat ``{agent: [epoch, ...]}`` with no subsystem key, so sharing
a file would make three enforcers' debounces silently consume each other's
budget and race on one atomic write.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .._reconcile._budget import Budget, DEFAULT_PASS_CAP, read_history, save_history
from ._banner import observe_pane
from ._resume import real_resume
from ._rule import Verdict, decide

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_PASS_CAP",
    "AgentReport",
    "PassOutcome",
    "SUBSYSTEM",
    "history_path",
    "resume_pass",
]

SUBSYSTEM = "rate-limit-resume"

#: Seconds between the two pane captures whose agreement defines "frozen".
#: Matched to the auth healer's default so the two sweeps judge "frozen" by
#: the same standard and cannot disagree about whether a pane is advancing.
DEFAULT_INTERVAL = 4.0

#: Explicit override for WHERE this enforcer's memory lives — SEPARATE from
#: both siblings'. See the module docstring.
_HISTORY_ENV = "SAC_RATE_LIMIT_HISTORY"

_BUDGET_VERDICTS = {
    "debounce": Verdict.COOLING_DOWN,
    "over-budget": Verdict.OVER_BUDGET,
    "pass-cap": Verdict.CAPPED,
}

#: Verdicts that SPEND budget: we touched the agent, whether or not it woke.
#: A failed nudge must cost the same as a successful one, or an agent that
#: cannot be woken is retried without limit.
_SPENT = (Verdict.RESUMED, Verdict.FAILED)


@dataclass(frozen=True)
class AgentReport:
    """One agent, one verdict, and WHY — in machine and human form."""

    name: str
    verdict: Verdict
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PassOutcome:
    """Everything one pass concluded and did."""

    reports: tuple[AgentReport, ...] = ()
    alarm: Any = None
    heartbeat_ok: bool = False
    applied: bool = False

    def of(self, *verdicts: Verdict) -> tuple[AgentReport, ...]:
        return tuple(r for r in self.reports if r.verdict in verdicts)

    def counts(self) -> dict[str, int]:
        out = {v.value: 0 for v in Verdict}
        for report in self.reports:
            out[report.verdict.value] += 1
        return {k: v for k, v in out.items() if v}

    def exit_code(self) -> int:
        """0 clean · 1 we owed a resume and did not deliver it · 2 we were blind.

        WAITING IS ZERO, and that is the load-bearing line. A wall that has
        not lifted yet is the NORMAL state during a rate limit, not a fault:
        exiting non-zero there would put this unit into a permanent failed
        state for hours at a time, and an alarm that is always on is an alarm
        nobody reads. The pass is doing exactly its job when it holds.

        RESET_UNKNOWN is TWO, with UNREADABLE and BUDGET_UNKNOWN, because all
        three are unresolved readings rather than known-bad outcomes: we saw a
        wall and cannot say when it lifts, or we could not look at all, or we
        cannot read our own memory of what we already did. Each needs a human,
        and none may be logged as a healthy tick.

        The code is not shouted into a void: the ecosystem supervisor's
        ``PeriodicRunner`` persists it per run as ``exit_code`` / ``ok`` in
        ``~/.scitex/dev/runtime/periodic-executions.jsonl``, alongside a
        record for a run it could not START at all. See :mod:`._alarm` for
        why that log, and not this package's own event log, is the reader of
        record.

        SWITCH_UNVERIFIED joins the TWOs for the same reason RESET_UNKNOWN is
        there: we typed three steps into an agent's pane and the pane will not
        say whether the model changed. That is an unresolved reading, not a
        known-bad outcome, and it is the one state of this remedy a human must
        actually look at.
        """
        if self.of(
            Verdict.UNREADABLE,
            Verdict.RESET_UNKNOWN,
            Verdict.BUDGET_UNKNOWN,
            Verdict.SWITCH_UNVERIFIED,
        ):
            return 2
        if self.of(
            Verdict.FAILED,
            Verdict.OVER_BUDGET,
            Verdict.COOLING_DOWN,
            Verdict.CAPPED,
            Verdict.WOULD_RESUME,
            Verdict.SWITCH_FAILED,
            Verdict.WOULD_SWITCH,
        ):
            return 1
        return 0


def history_path() -> Path:
    """Where this enforcer remembers what it already woke."""
    override = os.environ.get(_HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "rate-limit-resume-history.json"


def _real_captures(interval: float) -> dict[str, tuple[str | None, str | None]]:
    from .._authheal._detect import capture_live_panes

    return capture_live_panes(interval)


def _perform(
    name: str,
    decision: Any,
    *,
    budget: Budget | None,
    apply: bool,
    now: float,
    resume_fn: Callable[[str], bool],
) -> AgentReport:
    """Turn one authorised RESUME into what actually happened to it."""
    if budget is None:
        return AgentReport(
            name,
            Verdict.BUDGET_UNKNOWN,
            "budget-unreadable",
            f"{name}'s wall has lifted, but this pass cannot read its own "
            f"restart ledger, so it cannot honour the debounce. Refusing to "
            f"wake anything on a budget it cannot enforce",
        )
    check = budget.check(name, now)
    if not check.allowed:
        return AgentReport(
            name,
            _BUDGET_VERDICTS[check.reason],
            check.reason,
            f"{decision.detail} — but {check.detail}",
        )
    if not apply:
        return AgentReport(
            name, Verdict.WOULD_RESUME, decision.reason, decision.detail
        )
    woke = resume_fn(name)
    verdict = Verdict.RESUMED if woke else Verdict.FAILED
    detail = (
        f"{decision.detail} — resumed, and the payload was PROVEN to leave the "
        f"compose box"
        if woke
        else f"{decision.detail} — the nudge was NOT provably submitted, so "
        f"this agent is still parked. Recorded as degraded rather than "
        f"bounced again"
    )
    return AgentReport(name, verdict, decision.reason, detail)


def resume_pass(
    *,
    apply: bool = False,
    limit: int = DEFAULT_PASS_CAP,
    specs_dir: Path | None = None,
    history_file: Path | None = None,
    events_path: Path | None = None,
    alarm: bool = True,
    now: float | None = None,
    interval: float = DEFAULT_INTERVAL,
    capture_fn: Callable[[], dict[str, tuple[str | None, str | None]]] | None = None,
    resume_fn: Callable[[str], bool] | None = None,
    switch_model: bool = False,
    switch_fn: Callable[[str, str], bool | None] | None = None,
    switch_history_file: Path | None = None,
    default_tz: timezone = timezone.utc,
    err_stream: Any = None,
) -> PassOutcome:
    """Sweep the fleet for lifted rate walls and wake what is behind them.

    Parameters
    ----------
    apply
        Actually wake agents. Default ``False`` — detection is read-only, and
        the nudge is the only mutation in the whole flow.
    limit
        Cap on resumes in ONE pass: the blast radius of one bad tick.
    default_tz
        The frame a bare printed clock ("resets 8am") is read in when the
        provider printed no zone label. It is a SUBSTITUTION for a missing
        label, so it belongs to the caller and is never hidden in the parser.
    capture_fn, resume_fn
        The two live seams. Defaults are the real two-capture tmux read and
        the real verified delivery.
    switch_model
        Also handle the MODEL-CAP shape: an agent frozen behind a Fable cap
        is moved onto ``opus[1m]`` and kicked (:mod:`._switch_rule`,
        :mod:`._switch`). Default ``False``, and that default is
        load-bearing rather than shy — with it off this pass behaves
        EXACTLY as it did before the branch existed, down to the exit code,
        so enabling the switcher is a decision the operator makes and can
        unmake. ``--apply`` is still required for anything to be typed.
    switch_fn, switch_history_file
        The model-switch seams: the three-step mutation and the ledger that
        remembers whose model this pass already changed. That ledger is
        SEPARATE from the resume one — see :mod:`._switch_pass`.
    """
    from .._reconcile._pass import fleet_spec_paths
    from ..config import load_config
    from ._modelcap import observe_model_cap
    from ._switch import real_switch
    from ._switch_pass import (
        SWITCH_SPENT,
        already_switched,
        record_switch,
        switch_history_path,
        switch_report,
    )
    from ._switch_rule import decide_switch

    now = now if now is not None else time.time()
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    resume_fn = resume_fn if resume_fn is not None else real_resume
    switch_fn = switch_fn if switch_fn is not None else real_switch
    history_file = history_file if history_file is not None else history_path()
    stream = err_stream if err_stream is not None else sys.stderr

    # PROVE we can read our own memory before acting on it. A budget we
    # cannot read is not a budget, and treating an unreadable ledger as an
    # empty one would silently disarm every rate limit on a permission error.
    read = read_history(history_file)
    budget = Budget(read.history, pass_cap=limit) if read.enforceable else None
    if budget is None:
        print(f"[{SUBSYSTEM}] REFUSING to wake anything: {read.detail}", file=stream)

    # The model-switch remedy carries its OWN ledger, read only when the
    # remedy is armed. Two remedies sharing one flat ``{agent: [epoch, ...]}``
    # file would consume each other's budget: a resume would then look, to
    # this branch, like a switch it had already performed.
    switch_history_file = (
        switch_history_file
        if switch_history_file is not None
        else switch_history_path()
    )
    switch_budget: Budget | None = None
    if switch_model:
        switch_read = read_history(switch_history_file)
        switch_budget = (
            Budget(switch_read.history, pass_cap=limit)
            if switch_read.enforceable
            else None
        )
        if switch_budget is None:
            print(
                f"[{SUBSYSTEM}] REFUSING to switch any model: "
                f"{switch_read.detail}",
                file=stream,
            )

    # A capture that RAISED is not an empty fleet. Carrying that distinction is
    # what makes the rule's ``sessions-unreadable`` leg reachable from
    # production rather than from tests alone — a branch only a test can reach
    # is a branch nobody has evidence about.
    #
    # Note the ASYMMETRY with the empty-but-successful case, which is
    # deliberate and not an oversight: ``_list_tui_sessions`` returns ``[]``
    # both for a quiet host and for a tmux it could not read, and those are
    # indistinguishable from here. That ambiguity is TOLERABLE for this pass
    # and only for this pass, because it acts solely on agents it can SEE a
    # frozen banner on — so every way of failing to look removes candidates
    # rather than adding them, and blindness can only cost inaction. The
    # sibling ``fleet-reconcile`` cannot afford the same assumption, which is
    # why it carries a blackout breaker and this does not.
    sessions_readable = True
    # stx-allow: fallback (reason: a raising pane capture must become the rule's UNREADABLE verdict for every agent, never an empty fleet that reads as "nobody is walled". The refusal is NOT swallowed: each agent gets an UNREADABLE AgentReport in this pass's `reports`, which reaches the `sac agents resume-rate-limited` stdout line from cli_pkg/_agents_resume_rate_limited.py:_print_report (the systemd journal for a timer-driven run), the pass-record `counts` in runtime_root()/sac-events.jsonl via ._alarm.record_pass_completed, and exit code 2 from PassOutcome.exit_code, which the ecosystem supervisor's PeriodicRunner persists as `exit_code`/`ok` in ~/.scitex/dev/runtime/periodic-executions.jsonl)
    try:
        captures = (capture_fn or (lambda: _real_captures(interval)))()
    except Exception as exc:
        sessions_readable = False
        captures = {}
        print(
            f"[{SUBSYSTEM}] could not read the fleet's panes: {exc}",
            file=stream,
        )

    reports: list[AgentReport] = []
    for spec in fleet_spec_paths(specs_dir):
        # stx-allow: fallback (reason: one malformed or foreign spec.yaml must not abort the fleet sweep, mirroring `sac agents reconcile`. The failure is NOT swallowed: it becomes an UNREADABLE AgentReport in this pass's `reports`, which reaches three named sinks — the `sac agents resume-rate-limited` stdout line rendered by cli_pkg/_agents_resume_rate_limited.py:_print_report (the systemd journal, for the timer-driven run), the pass-record `counts` written to runtime_root()/sac-events.jsonl by ._alarm.record_pass_completed, and exit code 2 via PassOutcome.exit_code, which the ecosystem supervisor's PeriodicRunner persists as `exit_code`/`ok` in ~/.scitex/dev/runtime/periodic-executions.jsonl)
        try:
            config = load_config(spec)
        except Exception as exc:
            reports.append(
                AgentReport(
                    spec.parent.name,
                    Verdict.UNREADABLE,
                    "spec-unreadable",
                    f"could not read {spec}: {exc}",
                )
            )
            continue

        name = config.name
        policy = config.restart.policy
        pane1, pane2 = captures.get(name, (None, None))
        session_present = (name in captures) if sessions_readable else None
        decision = decide(
            name=name,
            policy=policy,
            session_present=session_present,
            first=observe_pane(pane1, now=moment, default_tz=default_tz),
            second=observe_pane(pane2, now=moment, default_tz=default_tz),
            now=moment,
        )

        # THE MODEL-CAP BRANCH. Evaluated only when armed, so with the flag
        # off this pass is byte-for-byte the pass it was before the branch
        # existed. Two ways it can change the outcome, and no third:
        #   * it FIRES — the agent is frozen behind a cap on a switchable
        #     model, and the switch replaces today's verdict entirely; or
        #   * today's rule found NOTHING to say ("no rate wall") while a cap
        #     IS visibly rendered, in which case reporting "nothing to do"
        #     would be the silence this whole command exists to abolish.
        # In every other case today's verdict stands untouched.
        if switch_model:
            switch = decide_switch(
                name=name,
                policy=policy,
                session_present=session_present,
                spec_model=config.claude.model,
                first=observe_model_cap(pane1, now=moment, default_tz=default_tz),
                second=observe_model_cap(pane2, now=moment, default_tz=default_tz),
                now=moment,
                already_switched=already_switched(switch_budget, name, now),
            )
            if switch.fires:
                done = switch_report(
                    name,
                    switch,
                    budget=switch_budget,
                    apply=apply,
                    now=now,
                    switch_fn=switch_fn,
                )
                if switch_budget is not None and done.verdict in SWITCH_SPENT:
                    record_switch(
                        switch_history_file,
                        switch_budget,
                        name=name,
                        now=now,
                        stream=stream,
                    )
                reports.append(
                    AgentReport(name, done.verdict, done.reason, done.detail)
                )
                continue
            if (
                decision.verdict is Verdict.NOT_LIMITED
                and switch.verdict is not Verdict.NOT_LIMITED
            ):
                reports.append(
                    AgentReport(name, switch.verdict, switch.reason, switch.detail)
                )
                continue

        if decision.verdict is not Verdict.RESUME:
            reports.append(
                AgentReport(name, decision.verdict, decision.reason, decision.detail)
            )
            continue
        report = _perform(
            name,
            decision,
            budget=budget,
            apply=apply,
            now=now,
            resume_fn=resume_fn,
        )
        if budget is not None and report.verdict in _SPENT:
            budget.record(name, now)
            # stx-allow: fallback (reason: a ledger write that fails must stop this pass from waking anything else — the alternative is an unbounded loop with no memory — but must not lose the reports already earned)
            try:
                save_history(history_file, budget.history, now=now)
            except OSError as exc:
                budget.spent = budget.pass_cap
                print(
                    f"[{SUBSYSTEM}] CANNOT RECORD resumes to {history_file}: {exc} "
                    f"— capping this pass so nothing is woken without memory",
                    file=stream,
                )
        reports.append(report)

    outcome = PassOutcome(reports=tuple(reports), applied=apply)
    if not alarm:
        return outcome
    from ._alarm import record_pass_completed, record_reports

    emitted = record_reports(outcome.reports, path=events_path, now=now)
    beat = record_pass_completed(
        outcome.counts(),
        mode="apply" if apply else "check",
        path=events_path,
        now=now,
    )
    return PassOutcome(
        reports=outcome.reports, alarm=emitted, heartbeat_ok=beat, applied=apply
    )
