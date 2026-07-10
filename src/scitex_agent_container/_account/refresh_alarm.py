"""Loud operator alerting for failed headless account refreshes.

INCIDENT 2026-07-10 (card
``incident-account-pool-all-expired-boot-failure-20260710``): the
host-side ``sac.accounts-refresh`` systemd timer had been receiving
failed refresh results for stored accounts for HOURS (the OAuth token
endpoint had moved and every grant 404'd) and told nobody — the
failures went to the journal each run, nothing pushed at the operator,
and the first visible symptom was ``sac agents start --group infra``
failing for every agent with ``NoHealthyAccountError``.

This module turns a failed refresh into an IMMEDIATE push on the
fleet's existing agent→lead ``blocker`` rail (ADR-0013,
:func:`scitex_agent_container._state.lead_inbox.push_to_lead` — the
same typed event agents already use for "creds expired", persisted
durably in the lead listen's ``channel_events`` store and relayed to
the operator by the lead session). No new delivery rail is invented.

Dedupe contract (operator spec):

* First failure for an account → ONE alert, recorded (with the failure
  class + time) in a small JSON state file under the runtime dir
  (``~/.scitex/agent-container/runtime/refresh-alarm-state.json``).
* Subsequent failing runs for the SAME already-alerted account → no
  re-alert (the timer fires every ~2h; a persistent failure would
  otherwise page the operator 12x/day).
* A successful refresh (or a skipped-still-fresh result) CLEARS the
  account's entry — an account that recovers and later dies again
  alerts again.
* A FAILED alert delivery is NOT recorded, so the next run retries it;
  the delivery failure itself is printed loudly to stderr.

:func:`alert_failed_refreshes` never raises — alerting is a side rail
and must never break the refresh run that feeds it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

STATE_FILENAME = "refresh-alarm-state.json"

# One-line recovery recipe included in every alert (operator spec: the
# canonical fix line must ride with the alert). Only meaningful for the
# token-dead classes; transport-class alerts carry their own "NOT a
# token problem" wording from ``refresh_account_credentials``.
RECOVERY_LINE = (
    "Recovery: `claude /login` as {name}, then `sac accounts save {name}` "
    "on the credential-holding host."
)


def _default_state_path(home: Path | None = None) -> Path:
    base = home if home is not None else Path.home()
    return base / ".scitex" / "agent-container" / "runtime" / STATE_FILENAME


def _load_state(path: Path) -> dict[str, Any]:
    """Read the alarm state file. Missing/corrupt reads as empty state."""
    # stx-allow: fallback (reason: a missing or corrupt dedupe-state file must degrade to "nothing alerted yet" — worst case one duplicate alert, never a crashed refresh run)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically persist the alarm state (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _notify_lead_blocker(summary: str, detail: str) -> None:
    """Leg 1 — the agent→lead ``blocker`` rail (ADR-0013).

    ``from_agent`` is ``$SAC_NAME`` when set (an in-container caller
    passes the ACL as itself); the host-timer context has no SAC_NAME
    and falls back to the LEAD's own name — a self-send, which the
    message:send ACL always admits, so the host rail can never be
    dropped by a group check. Raises (``LeadInboxError``) on any
    delivery failure.
    """
    from .._state.lead_inbox import push_to_lead, resolve_lead

    lead = resolve_lead()
    sender = os.environ.get("SAC_NAME", "").strip() or lead.name
    push_to_lead(
        kind="blocker",
        summary=summary,
        detail=detail,
        from_agent=sender,
        lead=lead,
    )


def _notify_todo_help_card(account: str, summary: str, detail: str) -> None:
    """Leg 2 — the scitex-todo BLOCKING-YOU card (first-tier nudge rail).

    Upserts the canonical ``help-<agent>-waiting`` card via
    :func:`scitex_todo._help_wait.help_wait` (idempotent; status
    ``blocked`` / blocker ``operator-decision``) under the pseudo-agent
    ``accounts-refresh-<account>``, so a dead account surfaces on the
    board's BLOCKING-YOU view and rides scitex-todo's own card-event
    delivery (digest/telegram). Lazy import — raises when scitex-todo
    is not installed, letting the caller report the rail as down.
    """
    from scitex_todo._help_wait import help_wait

    help_wait(
        agent=f"accounts-refresh-{account}",
        question=f"{summary}\n\n{detail}",
    )


def _default_notify(account: str, summary: str, detail: str) -> None:
    """Deliver through the EXISTING rails, most direct first.

    Leg 1 is the typed lead ``blocker`` push; a fleet without a
    ``lead:`` block (this host, 2026-07-11) falls through to leg 2, the
    scitex-todo help card — the board's standing "waiting on operator"
    rail. Raises only when EVERY leg failed, so the caller leaves the
    dedupe unmarked and retries next run.
    """
    # stx-allow: fallback (reason: multi-rail delivery — leg 1 being unconfigured/down must fall through to leg 2, not lose the alert; both failing raises loudly below)
    try:
        _notify_lead_blocker(summary, detail)
        return
    except Exception as lead_exc:  # stx-allow: fallback (reason: see inline comment)
        lead_err = lead_exc
    try:
        _notify_todo_help_card(account, summary, detail)
        return
    except Exception as todo_exc:  # stx-allow: fallback (reason: re-raised with full context — both rails' failures are surfaced together)
        raise RuntimeError(
            f"every alert rail failed — lead blocker rail: {lead_err}; "
            f"scitex-todo help-card rail: {todo_exc}"
        ) from todo_exc


def _build_summary(name: str, error: str) -> str:
    fix = RECOVERY_LINE.format(name=name)
    return (
        f"[sac accounts refresh] account '{name}' headless refresh FAILED — "
        f"{error} {fix}"
    )


def _build_detail(result: dict[str, Any], now_ts: float) -> str:
    name = str(result.get("name") or "?")
    lines = [
        f"account: {name}",
        f"credentials: {result.get('credentials_path')}",
        f"failure_kind: {result.get('failure_kind')}",
        f"error: {result.get('error')}",
        f"detected_at_epoch: {now_ts:.0f}",
        "impact: the sac.accounts-refresh timer can no longer keep this "
        "account's token fresh; agents pinned to it fail to boot once the "
        "current access token expires (NoHealthyAccountError).",
        RECOVERY_LINE.format(name=name),
    ]
    return "\n".join(lines)


def alert_failed_refreshes(
    results: list[dict[str, Any]],
    *,
    state_path: Path | str | None = None,
    notify: Callable[[str, str, str], None] | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> list[str]:
    """Alert the operator (via the lead blocker rail) about NEW failures.

    ``results`` is the per-account list ``sac accounts refresh`` builds
    (``{"name", "success", "skipped", "error", "failure_kind",
    "credentials_path", ...}``). Returns the account names alerted this
    run. Never raises.

    Parameters
    ----------
    state_path, notify, now, err_stream
        Test seams: an explicit dedupe-state file, a replacement
        delivery callable (called as ``notify(account, summary,
        detail)``), a fixed clock, and a replacement stderr.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    path = Path(state_path) if state_path is not None else _default_state_path()
    send = notify if notify is not None else _default_notify
    now_ts = now if now is not None else time.time()

    state = _load_state(path)
    alerted: list[str] = []
    dirty = False

    for result in results:
        name = str(result.get("name") or "").strip()
        if not name:
            continue
        ok = bool(result.get("skipped")) or bool(result.get("success"))
        if ok:
            # Recovery re-arms the alarm: a later death alerts again.
            if name in state:
                del state[name]
                dirty = True
            continue
        if name in state:
            continue  # already alerted for this outage — stay quiet
        error = str(result.get("error") or "unknown error")
        # stx-allow: fallback (reason: alert delivery failure must not crash the refresh run; it is reported loudly on stderr and retried on the next run because nothing is recorded)
        try:
            send(name, _build_summary(name, error), _build_detail(result, now_ts))
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            print(
                f"  {name:20s}  ALERT DELIVERY FAILED — {exc} "
                "(refresh failure NOT yet acknowledged; will retry on the "
                "next refresh run)",
                file=stream,
            )
            continue
        state[name] = {
            "alerted_at": now_ts,
            "failure_kind": result.get("failure_kind"),
            "error": error,
        }
        dirty = True
        alerted.append(name)
        print(
            f"  {name:20s}  ALERTED operator (lead blocker rail / "
            "scitex-todo help card; deduped until this account refreshes "
            "OK again)",
            file=stream,
        )

    if dirty:
        # stx-allow: fallback (reason: a dedupe-state write failure may cause one duplicate alert next run — preferable to crashing the refresh; the failure itself is printed loudly)
        try:
            _save_state(path, state)
        except OSError as exc:
            print(
                f"[refresh-alarm] failed to persist dedupe state at {path}: "
                f"{exc}",
                file=stream,
            )
    return alerted


__all__ = ["RECOVERY_LINE", "STATE_FILENAME", "alert_failed_refreshes"]
