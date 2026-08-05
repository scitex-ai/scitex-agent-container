"""One login-expired auto-restart pass over the live TUI fleet.

SIBLING OF ``_reconcile``, NOT A REPLACEMENT
    ``sac.fleet-reconcile`` restarts agents whose tmux session is GONE (a
    corpse: no session ⇒ no context to lose). It EXPLICITLY leaves alone a LIVE
    session whose Claude cannot authenticate — a frozen "Login expired" banner —
    because touching a live session destroys context. THIS pass owns exactly
    that other half: a live-but-auth-dead agent, which only a restart clears
    (Claude never re-reads its credentials). The frozen-banner corroboration
    (:mod:`._detect`) is what proves the session is wedged, not working, so the
    restart is safe and is the cure.

REUSE, NOT REINVENTION
    Reuses :mod:`.._reconcile._budget` wholesale — the rate limits proven in
    production by ``auth-heal.py`` (30-min/agent debounce, <=2/agent/hour,
    <=N/pass) — and :class:`.._reconcile._rule.Verdict`. It keeps its OWN history
    file (``SAC_LOGIN_EXPIRED_HISTORY``) so the two restarters' debounces stay
    independent and their atomic writes never race on one file.

POOL-LOADING RESTART (class fix, 2026-07-18)
    The restart goes through the normal :func:`.._lifecycle.lifecycle
    .agent_restart` path — the SAME mechanism ``sac.fleet-reconcile`` uses. With
    the pool class fix (:func:`..runtimes._envrc.resolve_secret_files`), that
    path now loads the CCT/Telegram token pool from the canonical ``$HOME``
    default even when ``SAC_SECRETS_ENVRC`` is unset, so a timer-driven restart
    can no longer strip an agent's bot token.

DEPLOY GATE — READ BEFORE ENABLING THE TIMER
    An existing ``auth-heal.py`` cron (its ``scan_tui``) ALREADY restarts these
    agents on the fleet host. Enabling this timer while that cron still runs =
    TWO restarters bouncing the same ``tui-<agent>`` sessions with INDEPENDENT
    debounce state = the double-supervisor class (the ``sac.listen`` catastrophe
    in another costume). This timer MUST NOT be enabled on a host until that
    host's ``auth-heal.py`` ``scan_tui`` is retired. See :mod:`.._jobs._jobs_plugin`
    and the ``sac agents restart-login-expired`` command help.

WHAT A CLEAN PASS IS ALLOWED TO MEAN
    A pass reports on the REGISTERED roster, not on whatever its pane reading
    happened to contain. Every registered agent must leave this pass in exactly
    one of three states — wedged, observed-and-fine, or UNOBSERVED — and only
    the wedged ones are ever restarted. That third state is what makes exit 0 a
    real claim rather than the far weaker "we produced no reports", which is
    also what a pass that read nothing at all produces.

    UNOBSERVED is not one thing, and the difference decides the exit code. A
    live session whose pane would not capture is US failing to look. A
    registered agent with NO session is a determinate reading of something this
    pass explicitly delegates to fleet-reconcile — and since the roster is spec
    files on a fleet that registers far more agents than it runs, treating it as
    an indeterminacy made exit 0 unreachable for every possible fleet state.
    See :meth:`PassOutcome.indeterminate`.

Every collaborator is an injectable seam with a REAL default, so tests drive the
whole pass against real panes, a real temp history file and a real temp event
log — with the one irreversible act (the restart) swapped for a recorder. No
mocks.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .._authevents import log_restart_attempted, log_restart_outcome
from .._reconcile._budget import (
    DEFAULT_PASS_CAP,
    Budget,
    read_history,
    save_history,
)
from .._reconcile._rule import Verdict
from ._alarm import record_pass_completed, record_reports
from ._detect import (
    DEFAULT_INTERVAL,
    capture_live_panes,
    detect_login_expired,
    registered_agents,
)
from ._observe import observe_wedge

# The report record and the NON-RESTART report constructors live in ._reports
# (extracted for the line cap); re-exported so existing imports of
# ``AgentReport`` from this module keep resolving.
from ._reports import (  # noqa: F401
    _ROSTER_SUBJECT,
    AgentReport,
    _roster_unreadable,
    _unobserved,
)

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_PASS_CAP",
    "AgentReport",
    "PassOutcome",
    "auth_heal_pass",
    "history_path",
]

#: Explicit override for WHERE this restarter's history lives — SEPARATE from
#: ``sac.fleet-reconcile``'s, so the two never race on one file.
_HISTORY_ENV = "SAC_LOGIN_EXPIRED_HISTORY"

#: Which rate-limit stood in the way, mapped to the verdict it becomes. Same
#: split as reconcile: only OVER-BUDGET gets a per-agent record (sac has stopped
#: trying); COOLING-DOWN is the NORMAL state of a healthy recovery and CAPPED is
#: our own throttle, so neither does — both are still counted in the pass record.
_BUDGET_VERDICTS = {
    "debounce": Verdict.COOLING_DOWN,
    "over-budget": Verdict.OVER_BUDGET,
    "pass-cap": Verdict.CAPPED,
}

#: Verdicts that mean we ATTEMPTED a restart, so the history must be persisted
#: before the next agent (a killed pass must not forget what it already bounced).
_SPENT = (Verdict.RESTARTED, Verdict.FAILED)


def history_path() -> Path:
    """Where the restart history lives. Resolved PER CALL, never cached."""
    override = os.environ.get(_HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "login-expired-restart-history.json"


def _real_restart(name: str) -> bool:
    """Restart ONE local agent through the pool-loading start path.

    ``agent_restart`` returns True on its own paths but forwards
    ``runtime.start(...)`` on another, and a runtime returning ``None`` must NOT
    be read as failure — inventing a false FAILURE is the mirror of the false
    SUCCESS that once sent the operator hunting a healthy credential store. So
    ``is not False`` (the same rule ``_reconcile._pass._real_restart`` uses).
    """
    from .._lifecycle.lifecycle import agent_restart

    return agent_restart(name) is not False


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

    def indeterminate(self) -> tuple[AgentReport, ...]:
        """UNOBSERVED reports that mean THIS pass could not determine something.

        Not every UNOBSERVED is an indeterminacy of ours. ``no-session`` is a
        DETERMINATE reading: there is no session, therefore there is no pane to
        be wedged, and :func:`_unobserved` already says whose problem it is —
        "a missing session is fleet-reconcile's half of the fleet, not ours".
        A pass cannot both delegate a case and let that case decide its answer.

        ``pane-unreadable`` and ``roster-unreadable`` are the real ones: a live
        session we failed to read, and not knowing who should have been read at
        all. Those are this pass failing at its own job.

        The load-bearing assumption, stated so it can be argued with: an agent
        wedged on auth still HAS its ``tui-`` session — it is sitting at a login
        prompt, which is exactly what the pane read detects. Sessionless means
        crashed or never started, a different failure with a different owner. If
        that ever stops holding, this method is where the bug will be.
        """
        return tuple(r for r in self.of(Verdict.UNOBSERVED) if r.reason != "no-session")

    def exit_code(self) -> int:
        """0 confirmed-clean · 1 something is wedged · 2 could-not-determine.

        0 is the STRONGEST claim this pass can make, so it is reserved for
        earning it: every agent this pass COULD observe was observed, and none
        of them is wedged. It is never the answer to "we produced no reports",
        because a pass that observed nothing at all produces no reports either —
        and while those two spelled the same 0, a wedged agent could sit for
        hours while the timer recorded a healthy tick for each pass that had
        failed to look at it.

        A genuine indeterminacy therefore outranks the clean answer and joins
        BUDGET_UNKNOWN at 2. They are one statement in two costumes — we could
        not determine the thing this pass exists to determine — and 2 is the
        code that lets a cron tell that apart from a fleet genuinely healthy.

        WHAT CHANGED, AND WHY. This used to count EVERY UNOBSERVED, including
        the registered-but-sessionless. The roster is spec FILES, and this fleet
        has far more registered agents than running ones by design, so every
        pass carried sessionless reports and the supervisor could NEVER return
        0 — not "rarely", never, for any fleet state whatsoever. A gate that
        cannot go green is a gate nobody reads, and the operator had already
        stopped reading this one. The count is not lost: :meth:`counts` still
        reports every UNOBSERVED, so a log line says "unobserved: 92" beside
        exit 0. What it can no longer do is impersonate "we failed to look".
        """
        if self.of(Verdict.BUDGET_UNKNOWN) or self.indeterminate():
            return 2
        if self.of(
            Verdict.FAILED,
            Verdict.OVER_BUDGET,
            Verdict.COOLING_DOWN,
            Verdict.CAPPED,
            Verdict.WOULD_RESTART,
        ):
            return 1
        return 0


def _perform(
    name: str,
    *,
    budget: Budget | None,
    apply: bool,
    now: float,
    restart_fn: Callable[[str], bool],
    budget_detail: str,
    account: str | None = None,
    event_log: Path | None = None,
) -> AgentReport:
    """Turn a corroborated login-expired agent into what we actually did.

    Emits the ATTEMPT and the OUTCOME to the auth-event log as two SEPARATE
    records (see :mod:`.._authevents`). They are deliberately not merged: this
    function is the exact place where intent and effect diverge — ``restart_fn``
    can raise, or return False, or return True over an agent that is still
    wedged — and a single "restarted" line cannot express that divergence. The
    emission is fail-open on both sides; a log that cannot be written must not
    cost us the restart.
    """
    base = f"{name} is login-expired (corroborated: a system auth banner frozen above its prompt across two captures)"
    if budget is None:
        # We could not read our OWN restart memory, so the debounce and the
        # hourly cap cannot be enforced. Restarting anyway would make EVERY
        # wedged agent restartable on EVERY tick — the loop the limits prevent.
        return AgentReport(
            name,
            Verdict.BUDGET_UNKNOWN,
            "budget-unreadable",
            f"{base}; NOT restarted: {budget_detail}",
        )
    check = budget.check(name, now)
    if not check.allowed:
        return AgentReport(
            name,
            _BUDGET_VERDICTS[check.reason],
            check.reason,
            f"{base}; NOT restarted: {check.detail}",
        )
    if not apply:
        return AgentReport(
            name,
            Verdict.WOULD_RESTART,
            "login-expired",
            f"{base} — would restart (dry-run/--check: nothing was done; re-run "
            f"with --apply to actually restart)",
        )
    # The ATTEMPT is recorded BEFORE the act, so a restart that hangs, or that
    # takes this process down with it, still leaves its intent on the record.
    attempt_id = log_restart_attempted(
        agent=name,
        account=account,
        detail=f"{base} — restarting to re-mount a live credential",
        path=event_log,
        now=now,
        extra={"source": "sac.restart-login-expired"},
    )
    # stx-allow: fallback (reason: one agent's restart raising must never abort the sweep — the rest of the wedged fleet still needs recovering; the failure is carded and reported)
    try:
        ok = restart_fn(name)
    except Exception as exc:
        budget.record(name, now)  # a restart we ATTEMPTED still spends budget
        log_restart_outcome(
            agent=name,
            attempt_id=attempt_id,
            succeeded=False,
            account=account,
            detail=f"restart RAISED and the agent was not recovered: {exc}",
            path=event_log,
            now=now,
        )
        return AgentReport(
            name, Verdict.FAILED, "restart-raised", f"{base}; restart FAILED: {exc}"
        )
    budget.record(name, now)
    # The OUTCOME is a separate record carrying the same attempt_id. Note what
    # ``succeeded`` claims and what it does not: the restart CALL reported
    # success. Whether the agent is actually authenticating again is decided by
    # the NEXT pass's reading, not by this line — which is why the attempt is
    # never retro-edited and the two records always both stand.
    log_restart_outcome(
        agent=name,
        attempt_id=attempt_id,
        succeeded=bool(ok),
        account=account,
        detail=(
            "restart call reported success (re-observation by a later pass is "
            "what confirms the wedge actually cleared)"
            if ok
            else "restart ran but reported FAILURE — the agent is still wedged"
        ),
        path=event_log,
        now=now,
    )
    if ok:
        return AgentReport(
            name,
            Verdict.RESTARTED,
            "login-expired",
            f"{base} — restarted to re-mount a live credential",
        )
    return AgentReport(
        name,
        Verdict.FAILED,
        "restart-returned-false",
        f"{base}; restart ran but reported FAILURE — the agent is still wedged",
    )


def auth_heal_pass(
    *,
    apply: bool = False,
    limit: int = DEFAULT_PASS_CAP,
    specs_dir: Path | None = None,
    history_file: Path | None = None,
    events_path: Path | None = None,
    alarm: bool = True,
    now: float | None = None,
    restart_fn: Callable[[str], bool] | None = None,
    capture_fn: Callable[[], dict] | None = None,
    interval: float = DEFAULT_INTERVAL,
    err_stream: Any = None,
    event_log: Path | None = None,
) -> PassOutcome:
    """Run ONE login-expired auto-restart pass over the live TUI fleet.

    ``apply=False`` (the default, selected by ``--check``) is a REPORT: it
    detects and decides but restarts nothing. The only board write a dry-run
    makes is this restarter's own heartbeat — and, since 2026-07-18, the
    auth-event records of what it OBSERVED. Observing is not acting: a dry run
    that saw a wedge really did see it, and that sighting is worth keeping.

    Parameters
    ----------
    specs_dir
        The fleet registry to read the roster from — the population every
        report is checked against. Real state, redirectable for tests, exactly
        as ``reconcile_pass`` takes it.
    event_log
        Override for the shared auth-event log (:mod:`.._authevents`). ``None``
        resolves the real runtime-dir path per call. This pass EMITS to that
        log and never reads it back: the log is a record for humans and later
        queries, never an input to this pass's decisions. Wiring it as an input
        would make the restarter's own history its evidence about itself.
    """
    now = now if now is not None else time.time()
    restart_fn = restart_fn if restart_fn is not None else _real_restart
    capture_fn = (
        capture_fn if capture_fn is not None else (lambda: capture_live_panes(interval))
    )
    history_file = history_file if history_file is not None else history_path()
    stream = err_stream if err_stream is not None else sys.stderr

    captures = capture_fn()
    roster = registered_agents(specs_dir)

    # A REGISTERED agent absent from the capture is missing from our READING of
    # the fleet, not from the fleet. Seeding it as an explicit (None, None)
    # turns that absence into a value the matcher classifies as UNKNOWN, rather
    # than a key that never exists and so can never be reported as anything.
    observed = dict(captures)
    for agent in roster.names:
        observed.setdefault(agent, (None, None))
    detection = detect_login_expired(observed)

    # PROVE we can read (and create) our own memory before acting on it — a
    # budget we cannot read is not a budget (see _reconcile._budget).
    read = read_history(history_file)
    budget = Budget(read.history, pass_cap=limit) if read.enforceable else None
    reports: list[AgentReport] = []

    for name in detection.auth_failed:
        # WHAT WE SAW, recorded before anything we do about it and regardless
        # of whether we are allowed to act — a wedge observed under --check, or
        # while cooling down, is the same fact as one observed before a
        # restart, and only the unbroken series of sightings can show that a
        # wedge outlived its remedy.
        account = observe_wedge(name, specs_dir=specs_dir, event_log=event_log, now=now)
        report = _perform(
            name,
            budget=budget,
            apply=apply,
            now=now,
            restart_fn=restart_fn,
            budget_detail=read.detail,
            account=account,
            event_log=event_log,
        )
        reports.append(report)
        # PERSIST THE MOMENT WE SPEND BUDGET, never only at the end: a pass
        # killed at its systemd timeout with history still in RAM would forget
        # what it just bounced and re-bounce it next tick, disarming the limits.
        if apply and budget is not None and report.verdict in _SPENT:
            # stx-allow: fallback (reason: if we can no longer RECORD restarts we must STOP performing them — an unrecordable restart is unbounded. Spending the pass cap halts further restarts safely; the loud print carries the failure.)
            try:
                save_history(history_file, budget.history, now=now)
            except OSError as exc:
                budget.spent = budget.pass_cap
                print(
                    f"[login-expired-restart] CANNOT RECORD restarts to "
                    f"{history_file} ({exc}) — halting this pass's restarts.",
                    file=stream,
                )

    if apply and budget is not None:
        # stx-allow: fallback (reason: the end-of-pass write is housekeeping; its failure is already reported per-restart above and must not crash a pass that has done its work)
        try:
            save_history(history_file, budget.history, now=now)
        except OSError:
            pass

    if budget is None and reports:
        print(
            f"[login-expired-restart] REFUSING to restart {len(reports)} wedged "
            f"agent(s): {read.detail}",
            file=stream,
        )

    # Now say what we did NOT manage to look at. These reports carry no action
    # — they are added after every restart decision precisely so they cannot
    # influence one — and they exist so that an agent this pass never read
    # leaves a line behind instead of leaving nothing behind.
    live = set(captures)
    if not roster.readable:
        reports.append(_roster_unreadable(roster.detail))
    reports.extend(_unobserved(name, live=name in live) for name in detection.unknown)

    outcome = PassOutcome(reports=tuple(reports), applied=apply)
    alarm_outcome = None
    heartbeat_ok = False
    if alarm:
        # Recording runs LAST, after every restart decision is made and carried
        # out, so nothing it does (or fails to do) can change what happened.
        alarm_outcome = (
            record_reports(reports, path=events_path, now=now, err_stream=err_stream)
            if apply
            else None
        )
        heartbeat_ok = record_pass_completed(
            outcome.counts(),
            mode="apply" if apply else "check",
            path=events_path,
            now=now,
            err_stream=err_stream,
        )
    return PassOutcome(
        reports=tuple(reports),
        alarm=alarm_outcome,
        heartbeat_ok=heartbeat_ok,
        applied=apply,
    )
