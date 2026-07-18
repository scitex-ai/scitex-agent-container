"""One reconcile pass: enumerate specs, observe tmux, resurrect corpses.

The IO half of the enforcer. The decision itself is pure and lives in
:mod:`._rule`; the rate limits in :mod:`._budget`; the board rails in
:mod:`._alarm`. This module only wires facts into the rule and carries out
whatever the rule authorises.

Every collaborator is an injectable seam with a REAL default, so tests
drive the whole pass against a real temp ``state.db``, a real temp
scitex-todo store and a real fake ``tmux`` — with the one and only
irreversible act (the restart) swapped for a recorder. No mocks.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ._alarm import (
    AlarmOutcome,
    clear_state_alarm,
    route_reports_to_cards,
    upsert_heartbeat,
    upsert_state_alarm,
)
from ._budget import DEFAULT_PASS_CAP, Budget, history_path, read_history, save_history
from ._rule import MANAGED_POLICIES, Verdict, decide

__all__ = [
    "AgentReport",
    "PassOutcome",
    "fleet_agents_dir",
    "fleet_spec_paths",
    "reconcile_pass",
]

#: Same override the sibling fleet-wide verb (``sac agents refresh-acl``)
#: honours, so both point at one registry in tests and on odd install roots.
_AGENTS_DIR_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


@dataclass(frozen=True)
class AgentReport:
    """One agent's line in the report. ``detail`` is ALWAYS printed."""

    name: str
    verdict: Verdict
    reason: str
    detail: str
    policy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "detail": self.detail,
            "policy": self.policy,
        }


@dataclass(frozen=True)
class PassOutcome:
    """Everything one pass concluded and did."""

    reports: tuple[AgentReport, ...] = ()
    alarm: AlarmOutcome | None = None
    heartbeat_ok: bool = False
    applied: bool = False

    def of(self, *verdicts: Verdict) -> tuple[AgentReport, ...]:
        return tuple(r for r in self.reports if r.verdict in verdicts)

    def counts(self) -> dict[str, int]:
        """Per-verdict tally — the heartbeat card's payload."""
        out = {v.value: 0 for v in Verdict}
        for report in self.reports:
            out[report.verdict.value] += 1
        return {k: v for k, v in out.items() if v}

    def exit_code(self) -> int:
        """0 clean · 1 something is down · 2 we were BLIND.

        UNKNOWN and BUDGET_UNKNOWN outrank everything because "I could not
        look" is not clean, and a pass that could not see the fleet — or
        could not read its own memory of what it already restarted — must
        not exit 0 and let a cron log it as a healthy tick.
        """
        if self.of(Verdict.UNKNOWN, Verdict.BUDGET_UNKNOWN):
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


def fleet_agents_dir() -> Path:
    """The user-scope fleet registry dir (``…/agents``).

    Globbed DIRECTLY rather than through :func:`config._resolve.resolve_config`
    — the resolver walks up from the CWD and raises ``AmbiguousRegistryScope``
    when a project-local registry is also visible. A scheduled enforcer must
    resolve the same fleet whatever directory systemd happens to start it in,
    so it takes the deterministic path, exactly as ``sac agents refresh-acl``
    already does for the same reason.

    Public because :mod:`.._authheal._detect` resolves the SAME roster to find
    the agents its pane reading failed to account for. Two sweeps of one fleet
    must never disagree about which agents exist, so they share this accessor
    rather than each growing their own.
    """
    override = os.environ.get(_AGENTS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import agents_root

    return agents_root()


def fleet_spec_paths(specs_dir: Path | None = None) -> list[Path]:
    """Every ``<name>/spec.yaml`` in the fleet registry, sorted.

    Skips ``_``-prefixed dirs (``_shared``, ``_template_*``, ``_archive``) —
    scaffolding, not fleet agents.
    """
    root = specs_dir if specs_dir is not None else fleet_agents_dir()
    if not root.is_dir():
        return []
    return [
        spec
        for spec in sorted(root.glob("*/spec.yaml"))
        if not spec.parent.name.startswith("_")
    ]


def _real_snapshot(*, socket_name: str | None = None) -> dict | None:
    from .._runners._tmux._tmux_probe import list_sessions_activity

    return list_sessions_activity(socket_name=socket_name)


def _batched_snapshot_fn(
    snapshot_fn: Callable[..., dict | None] | None,
) -> Callable[..., dict | None]:
    """Take the fleet's tmux snapshot ONCE per pass, not once per agent.

    ``tmux_session_observation`` probes per call, and at fleet scale that is
    ~93 ``tmux`` spawns a tick — the exact O(N)-subprocess cost that blew
    the heartbeat tick's budget and got it abandoned (see
    ``list_sessions_activity``'s docstring). The underlying probe is already
    batched (one ``tmux list-sessions`` returns every session), so we call
    it once and hand the cached answer to each agent's observation.

    A raising probe is cached as ``None`` — which ``_observed_snapshot``
    already maps to "I could not look", the correct three-state answer.
    """
    real = snapshot_fn if snapshot_fn is not None else _real_snapshot
    box: list[dict | None] = []

    def _cached(*, socket_name: str | None = None) -> dict | None:
        if not box:
            # stx-allow: fallback (reason: a probe that raises means we could not look; caching None routes that into the UNKNOWN branch rather than re-spawning a failing tmux once per agent)
            try:
                box.append(real(socket_name=socket_name))
            except Exception:
                box.append(None)
        return box[0]

    return _cached


def _real_restart(name: str) -> bool:
    """Restart ONE local agent. ``is not False`` per the CLI's own rule.

    ``agent_start`` returns True on its own paths but forwards
    ``runtime.start(...)`` on another, and a runtime returning ``None`` must
    NOT be read as failure — inventing a false FAILURE is the mirror of the
    false SUCCESS that once sent the operator hunting a healthy credential
    store. See ``cli_pkg/lifecycle/_restart.py``.
    """
    from .._lifecycle.lifecycle import agent_restart

    return agent_restart(name) is not False


def _local_host() -> str:
    """This machine's name AS ``instances.host`` records it.

    Deliberately the very function that WRITES that column
    (``record_instance_start`` → ``resolve_host``), so the comparison in the
    rule cannot drift from the stored value.
    """
    # stx-allow: fallback (reason: an unresolvable hostname must not manufacture a cross-host mismatch that skips the whole fleet — "" makes the rule's other-host guard stand down, and the remote=1 guard still holds)
    try:
        from .._state.state_db_hostname import resolve_host

        return str(resolve_host(None))
    except Exception:
        return ""


def _observe(config: Any, snapshot_fn, in_sif_fn) -> tuple[bool | None, bool | None]:
    """``(probe_ran, session_present)`` for this agent's tmux session."""
    from .._lifecycle._verdict_tmux import (
        session_name_for_config,
        tmux_session_observation,
    )

    return tmux_session_observation(
        session_name_for_config(config),
        snapshot_fn=snapshot_fn,
        in_sif_fn=in_sif_fn,
    )


def _spec_report(spec: Path, exc: Exception) -> AgentReport:
    """A spec we could not read is UNKNOWN — never a corpse.

    One malformed/foreign spec.yaml must not abort the fleet sweep, and it
    must not be guessed at either: if we cannot read the spec we cannot know
    whether sac ever promised to keep this agent running.
    """
    return AgentReport(
        name=spec.parent.name,
        verdict=Verdict.UNKNOWN,
        reason="spec-unreadable",
        detail=f"could not load {spec}: {exc} — cannot know whether this agent "
        f"should be running, so refusing to guess",
    )


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

#: Verdicts that mean we ATTEMPTED a restart, so the history must be
#: persisted before we touch the next agent. FAILED counts: a restart that
#: raised still consumed a real attempt, and forgetting it would let the
#: next tick retry immediately.
_SPENT = (Verdict.RESTARTED, Verdict.FAILED)


def _perform(
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


def reconcile_pass(
    *,
    apply: bool = False,
    limit: int = DEFAULT_PASS_CAP,
    specs_dir: Path | None = None,
    db_path: Path | None = None,
    history_file: Path | None = None,
    store: str | None = None,
    alarm: bool = True,
    now: float | None = None,
    restart_fn: Callable[[str], bool] | None = None,
    snapshot_fn: Callable[..., dict | None] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
    local_host_fn: Callable[[], str] | None = None,
    err_stream: Any = None,
) -> PassOutcome:
    """Run ONE reconcile pass over the fleet.

    ``apply=False`` (the default) is a REPORT: it reads tmux and the
    registry, decides, and mutates no agent. The only board write a dry-run
    makes is the reconciler's own heartbeat — a liveness beacon about US,
    never about an agent.

    Parameters
    ----------
    apply
        Actually restart. Default ``False``.
    limit
        Global cap on restarts THIS pass (blast radius of one bad tick).
    specs_dir, db_path, history_file, store
        Real state, redirectable for tests.
    restart_fn
        ``(name) -> bool``. The one irreversible act, injectable so tests
        can assert it was never called.
    snapshot_fn, in_sif_fn, local_host_fn
        The tmux/host sensors — see :mod:`.._lifecycle._verdict_tmux`.
    """
    from ..config import load_config

    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    restart_fn = restart_fn if restart_fn is not None else _real_restart
    snapshot = _batched_snapshot_fn(snapshot_fn)
    local_host = (local_host_fn or _local_host)()
    history_file = history_file if history_file is not None else history_path()
    stream = err_stream if err_stream is not None else sys.stderr

    # PROVE we can read (and create) our own memory before acting on it.
    # A budget we cannot read is not a budget, and `except OSError: return
    # {}` would render "forbidden to read" identical to "nothing restarted
    # yet" — silently disarming every rate limit on a permission error.
    # `budget=None` propagates that refusal to every corpse this pass finds.
    read = read_history(history_file)
    budget = Budget(read.history, pass_cap=limit) if read.enforceable else None
    reports: list[AgentReport] = []

    for spec in fleet_spec_paths(specs_dir):
        # stx-allow: fallback (reason: a single malformed/foreign spec.yaml must NOT abort the rest of the fleet sweep — it is reported as UNKNOWN and the sweep continues, mirroring `sac agents refresh-acl`)
        try:
            config = load_config(spec)
        except Exception as exc:
            reports.append(_spec_report(spec, exc))
            continue

        name = config.name
        policy = config.restart.policy
        probe_ran, present = _observe(config, snapshot, in_sif_fn)
        row = None
        if policy in MANAGED_POLICIES:
            from .._state.state_db_instances import last_known_instance

            row = last_known_instance(name, db_path=db_path)

        decision = decide(
            name=name,
            policy=policy,
            probe_ran=probe_ran,
            session_present=present,
            row=row,
            local_host=local_host,
        )
        if decision.verdict is not Verdict.RESTART:
            reports.append(
                AgentReport(
                    name, decision.verdict, decision.reason, decision.detail, policy
                )
            )
            continue
        report = _perform(
            name,
            decision,
            budget=budget,
            apply=apply,
            now=now,
            restart_fn=restart_fn,
            budget_detail=read.detail,
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

    if apply and budget is not None:
        # stx-allow: fallback (reason: the end-of-pass prune is housekeeping; its failure is already reported by the per-restart guard above and must not crash a pass that has already done its work)
        try:
            save_history(history_file, budget.history, now=now)
        except OSError:
            pass

    outcome = PassOutcome(reports=tuple(reports), applied=apply)
    # Every board rail is a SIDE rail: they run LAST, after every restart
    # decision is already made and carried out, so nothing they do (or fail
    # to do) can change what happened to the fleet.
    if alarm:
        # A reconciler that cannot read its own state must ALARM, never
        # quietly do nothing — "did nothing" is indistinguishable from
        # "nothing needed doing", which is the silence this whole command
        # exists to abolish. Cleared the moment the state is readable again.
        if not read.enforceable:
            upsert_state_alarm(
                read.detail,
                path=history_file,
                store=store,
                now=now_dt,
                err_stream=err_stream,
            )
            print(
                f"[fleet-reconcile] REFUSING to restart anything: {read.detail}",
                file=stream,
            )
        else:
            clear_state_alarm(store=store, err_stream=err_stream)
    alarm_outcome = (
        route_reports_to_cards(reports, store=store, now=now_dt, err_stream=err_stream)
        if (alarm and apply)
        else None
    )
    heartbeat_ok = (
        upsert_heartbeat(
            outcome.counts(),
            mode="apply" if apply else "dry-run",
            host=local_host,
            store=store,
            now=now_dt,
            err_stream=err_stream,
        )
        if alarm
        else False
    )
    return PassOutcome(
        reports=tuple(reports),
        alarm=alarm_outcome,
        heartbeat_ok=heartbeat_ok,
        applied=apply,
    )
