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
    host's ``auth-heal.py`` ``scan_tui`` is retired. See :mod:`.._jobs_plugin`
    and the ``sac agents restart-login-expired`` command help.

Every collaborator is an injectable seam with a REAL default, so tests drive the
whole pass against real panes, a real temp history file and a real scitex-todo
store — with the one irreversible act (the restart) swapped for a recorder. No
mocks.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .._reconcile._budget import (
    DEFAULT_PASS_CAP,
    Budget,
    read_history,
    save_history,
)
from .._reconcile._rule import Verdict
from ._alarm import route_reports_to_cards, upsert_heartbeat
from ._detect import DEFAULT_INTERVAL, capture_live_panes, detect_login_expired

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
#: split as reconcile: only OVER-BUDGET is carded (a human is genuinely needed);
#: COOLING-DOWN is the NORMAL state of a healthy recovery and CAPPED is our own
#: throttle, so neither mints a card.
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
class AgentReport:
    """One agent's line in the report. ``detail`` is ALWAYS printed."""

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
        """0 clean · 1 something is wedged · 2 we cannot read our own memory."""
        if self.of(Verdict.BUDGET_UNKNOWN):
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
) -> AgentReport:
    """Turn a corroborated login-expired agent into what we actually did."""
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
    # stx-allow: fallback (reason: one agent's restart raising must never abort the sweep — the rest of the wedged fleet still needs recovering; the failure is carded and reported)
    try:
        ok = restart_fn(name)
    except Exception as exc:
        budget.record(name, now)  # a restart we ATTEMPTED still spends budget
        return AgentReport(
            name, Verdict.FAILED, "restart-raised", f"{base}; restart FAILED: {exc}"
        )
    budget.record(name, now)
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
    history_file: Path | None = None,
    store: str | None = None,
    alarm: bool = True,
    now: float | None = None,
    restart_fn: Callable[[str], bool] | None = None,
    capture_fn: Callable[[], dict] | None = None,
    interval: float = DEFAULT_INTERVAL,
    err_stream: Any = None,
) -> PassOutcome:
    """Run ONE login-expired auto-restart pass over the live TUI fleet.

    ``apply=False`` (the default, selected by ``--check``) is a REPORT: it
    detects and decides but restarts nothing. The only board write a dry-run
    makes is this restarter's own heartbeat.
    """
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    restart_fn = restart_fn if restart_fn is not None else _real_restart
    capture_fn = (
        capture_fn if capture_fn is not None else (lambda: capture_live_panes(interval))
    )
    history_file = history_file if history_file is not None else history_path()
    stream = err_stream if err_stream is not None else sys.stderr

    names = detect_login_expired(capture_fn())

    # PROVE we can read (and create) our own memory before acting on it — a
    # budget we cannot read is not a budget (see _reconcile._budget).
    read = read_history(history_file)
    budget = Budget(read.history, pass_cap=limit) if read.enforceable else None
    reports: list[AgentReport] = []

    for name in names:
        report = _perform(
            name,
            budget=budget,
            apply=apply,
            now=now,
            restart_fn=restart_fn,
            budget_detail=read.detail,
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

    outcome = PassOutcome(reports=tuple(reports), applied=apply)
    alarm_outcome = None
    heartbeat_ok = False
    if alarm:
        # Board rails run LAST, after every restart decision is made and carried
        # out, so nothing they do (or fail to do) can change what happened.
        alarm_outcome = (
            route_reports_to_cards(
                reports, store=store, now=now_dt, err_stream=err_stream
            )
            if apply
            else None
        )
        heartbeat_ok = upsert_heartbeat(
            outcome.counts(),
            mode="apply" if apply else "check",
            store=store,
            now=now_dt,
            err_stream=err_stream,
        )
    return PassOutcome(
        reports=tuple(reports),
        alarm=alarm_outcome,
        heartbeat_ok=heartbeat_ok,
        applied=apply,
    )
