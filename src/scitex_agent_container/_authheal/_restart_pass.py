"""One NEAR-PROMPT login-required detect-and-restart pass, fully logged.

THE OPERATOR'S THREE LINES, IN ORDER
    1. automatically identify the agents showing login-required,
    2. run ``sac agents restart -y <agent>`` on each,
    3. log everything.

    Each line is a module: :mod:`._nearprompt` decides (1), :mod:`._restart_cmd`
    performs (2), :mod:`._journal` records (3), and this file is the sweep that
    puts them in that order. The third is not a courtesy — see the journal for
    why an unlogged restart is refused outright.

SIBLING OF ``_pass``, NOT A REPLACEMENT
    :func:`._pass.auth_heal_pass` restarts agents whose banner is
    DISTANCE-FROZEN across two captures, through the in-process
    ``agent_restart`` API. This pass exists because that freeze test misses the
    animating-but-wedged agents the operator then fixes by hand (the full
    argument is in :mod:`._nearprompt`). It differs in exactly two ways, both
    deliberate: the DISCRIMINATOR is near-prompt-only from a single capture, and
    the RESTART is the operator's own verified subprocess invocation with its
    return code, stdout and stderr written to the log in full.

    Everything else is REUSED: the roster (:func:`._detect.registered_agents`),
    the rate limits (:mod:`.._reconcile._budget`), the verdict vocabulary
    (:class:`.._reconcile._rule.Verdict`), the board rails (:mod:`._alarm`) and
    the report/outcome shapes (:mod:`._pass`). It keeps its OWN history file so
    its debounce cannot race the other two restarters' atomic writes on one path.

Every collaborator is an injectable seam with a REAL default, so a test drives
the whole pass against captured panes, a real temp history file, a real temp log
and a REAL executable script — with nothing mocked.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .._reconcile._budget import DEFAULT_PASS_CAP, Budget, read_history, save_history
from .._reconcile._rule import Verdict
from ._alarm import route_reports_to_cards, upsert_heartbeat
from ._detect import capture_live_panes_once, registered_agents
from ._journal import Journal
from ._nearprompt import (
    VERDICT_LOGIN_REQUIRED,
    VERDICT_UNKNOWN,
    Finding,
    classify_panes,
)
from ._pass import AgentReport, PassOutcome
from ._restart_cmd import RestartResult, run_sac_restart

__all__ = [
    "DEFAULT_PASS_CAP",
    "history_path",
    "restart_login_required_pass",
]

#: Explicit override for WHERE this restarter's history lives — SEPARATE from
#: both ``sac.fleet-reconcile``'s and ``restart-login-expired``'s, so the three
#: never race on one file and their debounces stay independent.
_HISTORY_ENV = "SAC_LOGIN_REQUIRED_HISTORY"

#: Same mapping as ``_pass``: only OVER-BUDGET is carded (a human is genuinely
#: needed); COOLING-DOWN is the normal state of a healthy recovery and CAPPED is
#: our own throttle, so neither mints a card.
_BUDGET_VERDICTS = {
    "debounce": Verdict.COOLING_DOWN,
    "over-budget": Verdict.OVER_BUDGET,
    "pass-cap": Verdict.CAPPED,
}

_ROSTER_SUBJECT = "<fleet-roster>"


def history_path() -> Path:
    """Where this pass's restart history lives. Resolved PER CALL, never cached."""
    override = os.environ.get(_HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "login-required-restart-history.json"


@dataclass
class _Sweep:
    """Mutable bookkeeping for one pass. Not shared, not thread-safe."""

    journal: Journal
    budget: Budget | None
    apply: bool
    now: float
    restart_fn: Callable[[str], RestartResult]
    budget_detail: str
    history_file: Path
    err_stream: Any
    reports: list[AgentReport] = field(default_factory=list)

    def log_finding(self, finding: Finding) -> None:
        """Write the verdict, the WHY, and the raw pane that produced them.

        Called for EVERY agent examined, healthy ones included. A log that
        records only the agents it acted on cannot answer "why was this one not
        restarted?" — the exact question the deployed script left unanswerable
        by putting that explanation in a state.db cache field instead.
        """
        self.journal.event(
            "EXAMINED",
            f"agent={finding.agent} verdict={finding.verdict.upper()} "
            f"why={finding.why} banner={finding.banner!r} "
            f"distance={finding.distance} prompt_found={finding.prompt_found} "
            f"ready={finding.ready}",
            now=self.now,
        )
        self.journal.event(
            "REASON", f"agent={finding.agent} {finding.detail}", now=self.now
        )
        if finding.pane is None:
            self.journal.event(
                "PANE",
                f"agent={finding.agent} NOT CAPTURED — no pane text exists to record",
                now=self.now,
            )
            return
        self.journal.block(
            "PANE",
            f"agent={finding.agent} verbatim capture:",
            finding.pane,
            now=self.now,
        )

    def log_restart(self, name: str, result: RestartResult) -> None:
        """Write the command, its exit code, and its FULL stdout and stderr."""
        self.journal.event(
            "RESTART-CMD", f"agent={name} argv={list(result.argv)!r}", now=self.now
        )
        self.journal.event(
            "RESTART-RESULT",
            f"agent={name} rc={result.returncode} duration={result.duration_s:.1f}s"
            + (f" error={result.error!r}" if result.error else ""),
            now=self.now,
        )
        self.journal.block(
            "RESTART-STDOUT", f"agent={name}", result.stdout, now=self.now
        )
        self.journal.block(
            "RESTART-STDERR", f"agent={name}", result.stderr, now=self.now
        )

    def handle(self, finding: Finding) -> None:
        """Decide, and when applying act, on ONE login-required agent."""
        name = finding.agent
        base = (
            f"{name} is login-required (near-prompt: the auth banner "
            f"{finding.banner!r} is the current UI state, {finding.distance} "
            f"non-chrome line(s) above the prompt)"
        )
        if self.budget is None:
            self.reports.append(
                AgentReport(
                    name,
                    Verdict.BUDGET_UNKNOWN,
                    "budget-unreadable",
                    f"{base}; NOT restarted: {self.budget_detail}",
                )
            )
            return
        check = self.budget.check(name, self.now)
        if not check.allowed:
            self.reports.append(
                AgentReport(
                    name,
                    _BUDGET_VERDICTS[check.reason],
                    check.reason,
                    f"{base}; NOT restarted: {check.detail}",
                )
            )
            return
        if not self.apply:
            self.reports.append(
                AgentReport(
                    name,
                    Verdict.WOULD_RESTART,
                    "login-required",
                    f"{base} — would run `sac agents restart -y {name}` "
                    f"(dry-run/--check: nothing was done; re-run with --apply "
                    f"to actually restart)",
                )
            )
            return
        if not self.journal.usable:
            # We cannot record what we are about to do, so we do not do it. An
            # unauditable restart is the failure this pass exists to end, and
            # performing one to "at least try" would reproduce it exactly.
            self.reports.append(
                AgentReport(
                    name,
                    Verdict.BUDGET_UNKNOWN,
                    "log-unwritable",
                    f"{base}; NOT restarted: {self.journal.detail}. Pin a "
                    f"writable log: export "
                    f"SAC_LOGIN_REQUIRED_LOG=/var/tmp/sac-login-required.log",
                )
            )
            return

        result = self.restart_fn(name)
        self.budget.record(name, self.now)  # an ATTEMPT spends budget
        self.log_restart(name, result)
        self._persist()

        if result.ok:
            self.reports.append(
                AgentReport(
                    name,
                    Verdict.RESTARTED,
                    "login-required",
                    f"{base} — `sac agents restart -y {name}` exited 0",
                )
            )
            return
        why = result.error or f"exit code {result.returncode}"
        self.reports.append(
            AgentReport(
                name,
                Verdict.FAILED,
                "restart-failed",
                f"{base}; `sac agents restart -y {name}` FAILED ({why}) — the "
                f"agent is still wedged. Its full stdout/stderr are in "
                f"{self.journal.path}",
            )
        )

    def _persist(self) -> None:
        """Save the history the MOMENT budget is spent, never only at the end.

        A pass killed at its systemd timeout with history still in RAM would
        forget what it just bounced and re-bounce it next tick, disarming the
        very limits it is carrying.
        """
        if self.budget is None:
            return
        # stx-allow: fallback (reason: if we can no longer RECORD restarts we
        # must STOP performing them — an unrecordable restart is unbounded.
        # Spending the pass cap halts further restarts safely.)
        try:
            save_history(self.history_file, self.budget.history, now=self.now)
        except OSError as exc:
            self.budget.spent = self.budget.pass_cap
            message = (
                f"CANNOT RECORD restarts to {self.history_file} ({exc}) — "
                f"halting this pass's restarts."
            )
            self.journal.event("HISTORY-WRITE-FAILED", message, now=self.now)
            print(f"[login-required] {message}", file=self.err_stream)


def _unobserved(finding: Finding) -> AgentReport:
    """An agent we took NO usable reading of — reported, never restarted."""
    return AgentReport(finding.agent, Verdict.UNOBSERVED, finding.why, finding.detail)


def _roster_unreadable(detail: str) -> AgentReport:
    """The ROSTER is what we could not read, so no pass can claim to be clean."""
    return AgentReport(
        _ROSTER_SUBJECT,
        Verdict.UNOBSERVED,
        "roster-unreadable",
        f"could not establish which agents SHOULD be running: {detail}. Agents "
        f"missing from this pass's reading cannot be told apart from agents "
        f"that do not exist, so this pass cannot report a clean fleet",
    )


def restart_login_required_pass(
    *,
    apply: bool = False,
    limit: int = DEFAULT_PASS_CAP,
    specs_dir: Path | None = None,
    history_file: Path | None = None,
    log_file: Path | None = None,
    store: str | None = None,
    alarm: bool = True,
    now: float | None = None,
    restart_fn: Callable[[str], RestartResult] | None = None,
    capture_fn: Callable[[], dict] | None = None,
    err_stream: Any = None,
) -> PassOutcome:
    """Detect login-required agents by NEAR-PROMPT and restart each, logging all.

    ``apply=False`` (the default) is a REPORT: it detects, decides and logs, but
    restarts nothing.
    """
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    restart_fn = restart_fn if restart_fn is not None else run_sac_restart
    capture_fn = capture_fn if capture_fn is not None else capture_live_panes_once
    history_file = history_file if history_file is not None else history_path()
    stream = err_stream if err_stream is not None else sys.stderr

    journal = Journal.open(log_file)
    journal.event(
        "PASS-START",
        f"mode={'apply' if apply else 'check'} limit={limit} "
        f"discriminator=near-prompt history={history_file}",
        now=now,
    )
    if not journal.usable:
        # The one message that cannot go in the log is the message that the log
        # does not work, so it goes to stderr.
        print(f"[login-required] NO LOG: {journal.detail}", file=stream)

    panes = capture_fn()
    roster = registered_agents(specs_dir)
    journal.event(
        "ROSTER",
        roster.detail if roster.readable else f"UNREADABLE — {roster.detail}",
        now=now,
    )

    # A REGISTERED agent absent from the capture is missing from our READING of
    # the fleet, not from the fleet. Seeding it explicitly as None turns that
    # absence into a value the discriminator classifies as UNKNOWN, rather than
    # a key that never exists and so can never be reported as anything at all.
    observed = dict(panes)
    for agent in roster.names:
        observed.setdefault(agent, None)

    findings = classify_panes(observed)
    journal.event(
        "EXAMINING",
        f"{len(findings)} agent(s) ({len(panes)} with a live tui- session, "
        f"{len(observed) - len(panes)} registered but not captured)",
        now=now,
    )

    # PROVE we can read (and create) our own memory before acting on it — a
    # budget we cannot read is not a budget (see _reconcile._budget).
    read = read_history(history_file)
    budget = Budget(read.history, pass_cap=limit) if read.enforceable else None
    if budget is None:
        journal.event("BUDGET", f"UNREADABLE — {read.detail}", now=now)

    sweep = _Sweep(
        journal=journal,
        budget=budget,
        apply=apply,
        now=now,
        restart_fn=restart_fn,
        budget_detail=read.detail,
        history_file=history_file,
        err_stream=stream,
    )

    for finding in findings:
        sweep.log_finding(finding)
    for finding in findings:
        if finding.verdict == VERDICT_LOGIN_REQUIRED:
            sweep.handle(finding)

    if apply and budget is not None:
        # stx-allow: fallback (reason: the end-of-pass write is housekeeping;
        # its failure is already reported per-restart above and must not crash a
        # pass that has already done its work)
        try:
            save_history(history_file, budget.history, now=now)
        except OSError:
            pass

    # Say what we did NOT manage to look at, AFTER every restart decision, so
    # nothing here can influence one.
    if not roster.readable:
        sweep.reports.append(_roster_unreadable(roster.detail))
    sweep.reports.extend(
        _unobserved(f) for f in findings if f.verdict == VERDICT_UNKNOWN
    )

    outcome = PassOutcome(reports=tuple(sweep.reports), applied=apply)
    counts = outcome.counts()
    journal.event(
        "PASS-END",
        f"examined={len(findings)} "
        + (" ".join(f"{k}={v}" for k, v in counts.items()) or "nothing-wedged")
        + f" exit={outcome.exit_code()}",
        now=now,
    )

    alarm_outcome = None
    heartbeat_ok = False
    if alarm:
        # Board rails run LAST, after every restart decision is made and carried
        # out, so nothing they do (or fail to do) can change what happened.
        alarm_outcome = (
            route_reports_to_cards(
                sweep.reports, store=store, now=now_dt, err_stream=err_stream
            )
            if apply
            else None
        )
        heartbeat_ok = upsert_heartbeat(
            counts,
            mode="apply" if apply else "check",
            store=store,
            now=now_dt,
            err_stream=err_stream,
        )
    return PassOutcome(
        reports=tuple(sweep.reports),
        alarm=alarm_outcome,
        heartbeat_ok=heartbeat_ok,
        applied=apply,
    )
