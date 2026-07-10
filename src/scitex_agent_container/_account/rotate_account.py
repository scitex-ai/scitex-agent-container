"""Mode B action: reactive per-agent account rotation (task #13 action layer).

Sibling of :mod:`_account.backoff_agent` (Mode A) — see that module's
docstring for the operator directive (op-2026-06-12-13) both implement.
Mode B is the SUSTAINED-cap case: the classifier decided the current
account is genuinely capped (a 429/403 on an already-high-usage
account, or an unambiguous textual/auth cap signal), so the agent
needs to move to a DIFFERENT account right now rather than wait out a
window that will not clear.

This module deliberately does NOT duplicate account selection or
credential-copy logic. It calls the two EXISTING primitives the
proactive ``sac accounts watch-quota`` loop already relies on:

* :func:`_account.quota_watch._select_next_account` — health-gated
  (non-expired credential snapshot; see the 2026-07-06 expired-account
  regression that primitive's docstring documents) pick of the
  lowest-5h-usage account that isn't the current one. Returns
  ``None`` when no healthy non-current candidate exists.
* :func:`_state.account_store.switch_account` — copies the picked
  account's credential snapshot into ``~/.claude/`` and appends a
  structured rotation-audit record (:mod:`_account._rotation_audit`).

The one thing THIS module adds on top of ``_select_next_account`` +
``switch_account`` is the REACTIVE framing: :func:`rotate_account` skips
the proactive threshold/``fetch_usage`` gate
:func:`_account.quota_watch.check_and_rotate` applies, because a
reactive caller already knows — from a live signal that has been run
through :func:`_account.rate_limit_classifier.classify_rate_limit_signal`
— that Mode B was decided. Re-running a threshold gate here would be
redundant and could disagree with the live signal (e.g. a stale quota
cache reading below-threshold while the account is demonstrably 429ing
right now).

Never raises — mirrors ``check_and_rotate``'s contract. The reactive
caller invokes this from inside an SDK-conversation exception handler;
a second exception escaping THIS function would mask the original
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._state.account_store import list_accounts, switch_account
from .credentials import read_credentials_metadata
from .quota_watch import _select_next_account

__all__ = ["ROTATE_EVENT", "RotateResult", "rotate_account"]

# Rotation-audit ``event`` value for a REACTIVE rotate (distinct from
# quota_watch's periodic ``"auto-rotate"`` so the audit trail — and any
# operator grepping it — can tell "polled over threshold" apart from
# "a live 429/403/textual/auth signal forced this"). Registered in
# ``_account._rotation_audit._KNOWN_EVENTS`` so it doesn't log as an
# unrecognised event on every reactive rotation.
ROTATE_EVENT = "reactive-rotate"

# Possible ``RotateResult.action`` values.
ACTION_ROTATED = "rotated"
ACTION_NO_ACCOUNTS = "no_accounts"


@dataclass(frozen=True)
class RotateResult:
    """Outcome of one :func:`rotate_account` call.

    Attributes
    ----------
    action
        ``"rotated"`` on a successful credential switch, ``"no_accounts"``
        when there was no healthy non-current candidate to rotate to
        (per ``_select_next_account``'s documented contract: stay put
        rather than rotate onto an unhealthy/expired account) or the
        switch itself failed.
    switched_to
        The stored-account name we switched to, or ``None``.
    from_account
        The email of the account we were rotating away from (best
        effort; ``None`` if unresolvable).
    message
        Human-readable summary for logs / session.jsonl events.
    """

    action: str
    switched_to: str | None
    from_account: str | None
    message: str


def rotate_account(
    *,
    reason: str,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> RotateResult:
    """Perform Mode B: rotate the CURRENT agent off its (capped) account.

    Parameters
    ----------
    reason
        WHY the rotation is happening (e.g. the triggering
        :class:`_account.rate_limit_signals.RateLimitSignal` + the
        matched pattern/status). Recorded verbatim on the rotation-audit
        record (see :mod:`_account._rotation_audit`).
    store_dir, home
        Overrides for the accounts store / user home directory. ``None``
        resolves via the same SciTeX local-state cascade
        ``_select_next_account`` / ``switch_account`` already use.
        Tests pass explicit ``tmp_path``-rooted values.
    now
        Wall-clock override (unix seconds) forwarded to
        ``_select_next_account``'s credential-freshness check. Tests
        only.

    Returns
    -------
    RotateResult

    Never raises.
    """
    # stx-allow: fallback (reason: this runs inside an SDK-conversation exception handler — a second exception here must never mask the original failure the caller is already handling)
    try:
        meta = read_credentials_metadata(home=home)
        current_email = meta.get("email_address")
        accounts: list[dict[str, Any]] = list_accounts(store_dir=store_dir, home=home)
        next_acct = _select_next_account(
            accounts, current_email, store_dir=store_dir, home=home, now=now
        )
        if next_acct is None:
            return RotateResult(
                action=ACTION_NO_ACCOUNTS,
                switched_to=None,
                from_account=current_email,
                message=(
                    "reactive-rotate: no HEALTHY non-current account "
                    "available — staying put on "
                    f"{current_email or '(unknown account)'}."
                ),
            )
        switch_result = switch_account(
            next_acct["name"],
            store_dir=store_dir,
            home=home,
            event=ROTATE_EVENT,
            reason=reason,
            from_account=current_email,
        )
        if not switch_result.get("success"):
            return RotateResult(
                action=ACTION_NO_ACCOUNTS,
                switched_to=None,
                from_account=current_email,
                message=(
                    "reactive-rotate: switch_account failed: "
                    f"{switch_result.get('message')}"
                ),
            )
        return RotateResult(
            action=ACTION_ROTATED,
            switched_to=next_acct.get("name"),
            from_account=current_email,
            message=(
                f"reactive-rotate: switched from {current_email!r} to "
                f"{next_acct.get('name')!r} ({reason})"
            ),
        )
    except Exception as exc:  # stx-allow: fallback (reason: see function docstring — never raise into the caller's own exception handler)
        return RotateResult(
            action=ACTION_NO_ACCOUNTS,
            switched_to=None,
            from_account=None,
            message=f"reactive-rotate: unexpected error: {exc}",
        )
