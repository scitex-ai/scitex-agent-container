# -*- coding: utf-8 -*-
"""Reactive rate-limit wiring for the conversation supervisor (task #13).

Extracted from :mod:`._session_conversation` (mirrors the sibling
:mod:`._session_dead_recovery`) so the supervisor's outer ``except``
block stays a thin dispatcher and this file stays under the project's
per-file line cap.

:func:`handle_rate_limit_failure` is the supervisor's SECOND recovery
check — after ``handle_dead_session_resume`` (a stale ``--resume``
target), before the auth-expired / generic-``sdk-crash``
classification. Given the SAME ``enriched`` failure text every other
classifier in that ``except`` block already sees (``str(exc)`` folded
with captured subprocess stderr), it:

1. detects a REACTIVE rate-limit signal via
   :func:`_account.rate_limit_signals.detect_signal_from_text` (HTTP
   status text signatures, then textual cap markers);
2. joins it with the account's CACHED usage% via
   :func:`_account.rate_limit_classifier.classify_rate_limit_signal`
   to get Mode A (BACKOFF) vs Mode B (ROTATE);
3. dispatches to the action layer — :func:`_account.backoff_agent
   .backoff_agent` (Mode A) or :func:`_account.rotate_account
   .rotate_account` (Mode B) — and emits the ``account_backoff`` /
   ``account_rotated`` observability events via the SAME
   ``append_session_message`` (session.jsonl) / ``report_sdk_error``
   (``state.db.errors``) channels every other supervisor event in this
   file already uses. No new event mechanism invented.

Returns ``ReactiveOutcome(handled=False, ...)`` when ``enriched``
carries no rate-limit signal — the caller's existing auth/sdk-crash
classification applies completely unchanged; this module is a pure
no-op in that case (no event emitted, no state mutated).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .._account.backoff_agent import backoff_agent
from .._account.quota_cache import read_quota_entry
from .._account.rate_limit_classifier import (
    AccountUsageSnapshot,
    Mode,
    classify_rate_limit_signal,
)
from .._account.rate_limit_signals import RateLimitSignal, detect_signal_from_text
from .._account.rotate_account import rotate_account

logger = logging.getLogger(__name__)

__all__ = ["RATE_LIMITED_CAUSE", "ReactiveOutcome", "handle_rate_limit_failure"]

# Short ``cause`` identifier written to ``state.db.errors`` — distinct
# from ``auth-expired`` (``_auth_failure.AUTH_FAILURE_CAUSE``),
# ``dead-session`` (``_dead_session.DEAD_SESSION_CAUSE``), and the
# generic ``sdk-crash`` so the lead can group on it and the operator
# immediately sees "this was a provider rate-limit, auto-handled"
# rather than an ambiguous crash.
RATE_LIMITED_CAUSE = "rate-limited"


@dataclass(frozen=True)
class ReactiveOutcome:
    """What the supervisor loop should do after one exception.

    Attributes
    ----------
    handled
        ``False`` — ``enriched`` carried no rate-limit signal (or an
        internal detector error occurred); the caller's existing
        auth/sdk-crash classification applies unchanged.
    reset_attempt
        ``True`` only after a SUCCESSFUL Mode B rotation — the agent
        is now on a fresh account, so the caller should reset its
        ``attempt`` counter to 0 and retry IMMEDIATELY (same contract
        as ``handle_dead_session_resume``'s ``True`` return), without
        charging the ``max_restarts`` budget.
    delay_s
        Seconds the caller should wait before its next attempt, valid
        when ``handled`` is ``True`` and ``reset_attempt`` is
        ``False`` (Mode A, or Mode B with no healthy account to
        rotate to — "never park" falls back to a backoff-and-retry on
        the same account rather than giving up). ``None`` otherwise.
    consecutive_hits
        The new consecutive-Mode-A-hit count the caller must persist
        and pass back in as ``consecutive_hits`` on its NEXT call
        (see :func:`_account.backoff_agent.backoff_agent`). Always 0
        when ``handled`` is ``False`` or after a successful rotation.
    """

    handled: bool
    reset_attempt: bool
    delay_s: float | None
    consecutive_hits: int


def _not_handled() -> ReactiveOutcome:
    return ReactiveOutcome(
        handled=False, reset_attempt=False, delay_s=None, consecutive_hits=0
    )


def _rotated_outcome() -> ReactiveOutcome:
    return ReactiveOutcome(
        handled=True, reset_attempt=True, delay_s=0.0, consecutive_hits=0
    )


def _usage_snapshot() -> AccountUsageSnapshot:
    """Best-effort CACHED usage% for the current agent's account.

    Reads ``quota-cache.json`` (no network call — see
    :mod:`_account.quota_cache`) keyed by ``$CLAUDE_AGENT_ACCOUNT``.
    Degrades to the classifier's own zero-usage default when the
    cache is absent/stale/unreadable/has no matching entry. Zero is
    the SAFE default here (never over-eagerly rotate when the real
    usage% is unknown) — mirrors :class:`AccountUsageSnapshot`'s own
    field defaults.
    """
    entry = read_quota_entry()
    if entry is None:
        return AccountUsageSnapshot()
    return AccountUsageSnapshot(
        used_pct_5h=float(entry.get("h5") or 0.0),
        used_pct_7d=float(entry.get("d7") or 0.0),
    )


def _emit_error_event(
    *,
    state_dir: Path,
    name: str,
    host: str | None,
    enriched: str,
    attempt: int,
    stderr_event_fields: dict[str, Any],
    append_session_message: Callable[[Path, dict], None],
    report_sdk_error: Callable[..., Any],
    db_writer: Any,
) -> None:
    """Record the underlying failure — mirrors every other classifier
    in the supervisor's except block: auto-recovering from a
    rate-limit hit does not make it any less worth recording."""
    append_session_message(
        state_dir,
        {
            "type": "error",
            "kind": "rate_limited",
            "detail": enriched,
            "attempt": attempt,
            **stderr_event_fields,
        },
    )
    if host:
        report_sdk_error(
            name=name,
            host=host,
            cause=RATE_LIMITED_CAUSE,
            detail=enriched,
            db_writer=db_writer,
        )


def _try_rotate(
    *,
    signal: RateLimitSignal,
    pattern: str,
    reason_suffix: str,
    state_dir: Path,
    append_session_message: Callable[[Path, dict], None],
    account_home: Path | None,
    account_store_dir: Path | None,
):
    """Call the Mode B action layer and emit its outcome event.

    ``account_home`` / ``account_store_dir`` are test-injection seams
    forwarded verbatim to :func:`_account.rotate_account.rotate_account`
    (``None`` in production — the runner process's own ``$HOME`` IS the
    agent's credential/account-store home, so no override is needed
    there; tests pass explicit ``tmp_path``-rooted values so this NEVER
    touches a real ``~/.claude`` / ``~/.scitex`` directory).

    Returns the raw :class:`_account.rotate_account.RotateResult` so
    the caller can branch on ``.action``.
    """
    result = rotate_account(
        reason=f"reactive {signal.value} (pattern={pattern!r}) {reason_suffix}",
        home=account_home,
        store_dir=account_store_dir,
    )
    append_session_message(
        state_dir,
        {
            "type": "supervisor",
            "event": (
                "account_rotated" if result.action == "rotated" else "account_rotate_skipped"
            ),
            "signal": signal.value,
            "pattern": pattern,
            "switched_to": result.switched_to,
            "from_account": result.from_account,
            "detail": result.message,
        },
    )
    return result


def handle_rate_limit_failure(
    *,
    enriched: str,
    state_dir: Path,
    name: str,
    host: str | None,
    attempt: int,
    consecutive_hits: int,
    stderr_event_fields: dict[str, Any],
    append_session_message: Callable[[Path, dict], None],
    report_sdk_error: Callable[..., Any],
    db_writer: Any,
    account_home: Path | None = None,
    account_store_dir: Path | None = None,
) -> ReactiveOutcome:
    """Detect + auto-handle a reactive rate-limit failure, in place.

    ``account_home`` / ``account_store_dir`` are forwarded verbatim to
    every Mode B rotation attempt — see :func:`_try_rotate`. Both
    default to ``None`` (production: the runner process's own
    ``$HOME``); tests pass explicit ``tmp_path``-rooted values.

    See module docstring for the full flow. Never raises — mirrors
    every other classifier in the supervisor's ``except`` block: a
    detector bug here must never mask the ORIGINAL SDK failure it is
    trying to classify, so any internal error degrades to
    ``handled=False`` (fall through to the caller's generic handling).
    """
    # stx-allow: fallback (reason: see function docstring — must never raise into the supervisor's own exception handler)
    try:
        hit = detect_signal_from_text(enriched)
        if hit is None:
            return _not_handled()
        signal, pattern = hit
        # NONE is currently unreachable for any signal
        # detect_signal_from_text can produce (HTTP_429/403 are always
        # BACKOFF-or-ROTATE, HTTP_529 is always BACKOFF, TEXTUAL_MATCH
        # is always ROTATE) — kept as a defensive fall-through in case
        # the detector grows a new signal type later.
        mode = classify_rate_limit_signal(signal, _usage_snapshot())
        if mode is Mode.NONE:
            return _not_handled()

        _emit_error_event(
            state_dir=state_dir,
            name=name,
            host=host,
            enriched=enriched,
            attempt=attempt,
            stderr_event_fields=stderr_event_fields,
            append_session_message=append_session_message,
            report_sdk_error=report_sdk_error,
            db_writer=db_writer,
        )
        logger.warning(
            "claude-session RATE LIMITED for %s: signal=%s pattern=%r mode=%s",
            name,
            signal.value,
            pattern,
            mode.value,
        )

        already_tried_rotate = False
        if mode is Mode.ROTATE:
            result = _try_rotate(
                signal=signal,
                pattern=pattern,
                reason_suffix=f"attempt={attempt}",
                state_dir=state_dir,
                append_session_message=append_session_message,
                account_home=account_home,
                account_store_dir=account_store_dir,
            )
            already_tried_rotate = True
            if result.action == "rotated":
                return _rotated_outcome()
            # No healthy account to rotate to — "never park": fall
            # through to Mode A so the agent still retries instead of
            # dying on the (still-capped) current account.

        decision = backoff_agent(prior_consecutive_hits=consecutive_hits)
        if decision.escalate_to_rotate and not already_tried_rotate:
            result = _try_rotate(
                signal=signal,
                pattern=pattern,
                reason_suffix=(
                    f"escalated after {decision.hit_count} consecutive backoffs"
                ),
                state_dir=state_dir,
                append_session_message=append_session_message,
                account_home=account_home,
                account_store_dir=account_store_dir,
            )
            if result.action == "rotated":
                return _rotated_outcome()

        append_session_message(
            state_dir,
            {
                "type": "supervisor",
                "event": "account_backoff",
                "signal": signal.value,
                "pattern": pattern,
                "delay_s": decision.delay_s,
                "hit_count": decision.hit_count,
            },
        )
        return ReactiveOutcome(
            handled=True,
            reset_attempt=False,
            delay_s=decision.delay_s,
            consecutive_hits=decision.hit_count,
        )
    except Exception as exc:  # stx-allow: fallback (reason: see function docstring)
        logger.warning(
            "handle_rate_limit_failure: internal error (falling through to "
            "generic sdk-crash classification): %s",
            exc,
        )
        return _not_handled()


# EOF
