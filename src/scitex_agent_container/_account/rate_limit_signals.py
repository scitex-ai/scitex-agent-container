"""Rate-limit / quota-cap signal taxonomy and parsers (task #13).

Operator directive op-2026-06-12-13 (Telegram 12676 → 12677): the
"is this account capped?" question is NOT a single-HTTP-code
question. The lead's classifier consumes signals from MULTIPLE
channels in OR:

* PROACTIVE — usage-% from the existing ``quota-cache.json`` /
  ``sac accounts list --json`` store. Catches caps BEFORE the SDK
  starts returning errors.
* REACTIVE HTTP status — ``429`` (rate limit), ``403`` (some
  providers' cap shape), ``529`` (Anthropic overload).
* REACTIVE TEXTUAL — substring / regex match in session.jsonl or
  runner stderr/stdout. Real-world cap phrasing from the field:
  ``"You've hit your weekly limit · resets ..."`` (observed on an
  agent account 2026-06-12 by operator).
* REACTIVE AUTH-EVENT — the existing ``_runners/_auth_failure``
  hook emits an ``account_capped`` status the SDK runner already
  surfaces. We subscribe to that channel rather than re-parsing
  the underlying error.

This module owns the *typing* of those signals; the
*classification* (Mode A "transient backoff" vs Mode B "sustained
cap → rotate") lives in
:mod:`scitex_agent_container._account.rate_limit_classifier` —
that split keeps the signal taxonomy pure (no usage-% lookup,
no time-window state) so it can be tested with literal input.

Public surface:

* :class:`RateLimitSignal` — closed enum of normalized signal
  kinds. New signal types extend this enum (the classifier's
  ``match`` covers the new case explicitly); detector parsers map
  their domain-specific input to one of these values.
* :func:`classify_http_status(status)` — map HTTP status int to
  the corresponding :class:`RateLimitSignal` or ``None``. Pure.
* :func:`scan_textual_cap_markers(text, patterns=DEFAULT_PATTERNS)`
  — case-insensitive regex scan over a multi-line blob. Returns
  ``RateLimitSignal.TEXTUAL_MATCH`` on any hit (or ``None``).
* :data:`DEFAULT_TEXTUAL_PATTERNS` — operator-tunable list of
  case-insensitive regex strings. Driven by data, not code, so
  adding "Xiaomi quota exhausted" or "DeepSeek 402 insufficient
  balance" is a one-line append, not a code review.

Out of scope (deferred):

* Per-provider HTTP error class taxonomy — the dispatcher (PR-B
  ``_runners/_provider_dispatch``) owns 401/402/500-503 etc;
  THIS module concerns itself only with the subset that signals
  ACCOUNT cap (vs network blip, vs operator bug).
* Action layer — Mode A backoff_agent and Mode B rotate_account
  are sibling modules; this file feeds them through the
  classifier.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterable


class RateLimitSignal(str, Enum):
    """Normalized signal kinds the classifier consumes.

    str-enum so the value serialises cleanly into observability
    events (``provider_fallback`` / ``account_rotated`` JSON
    payloads stay human-readable in operator log dumps without a
    custom encoder).
    """

    USAGE_PCT_5H = "usage_pct_5h"
    """The proactive 5h-window utilisation from the quota cache."""

    USAGE_PCT_7D = "usage_pct_7d"
    """The proactive 7d-window utilisation from the quota cache."""

    HTTP_429 = "http_429"
    """Rate-limit HTTP status from the provider."""

    HTTP_403 = "http_403"
    """Forbidden — some providers' cap shape (Anthropic uses 429,
    OpenAI sometimes returns 403 for org-limit exceedance)."""

    HTTP_529 = "http_529"
    """Anthropic overload (server-capacity throttle — transient)."""

    TEXTUAL_MATCH = "textual_match"
    """A pattern from :data:`DEFAULT_TEXTUAL_PATTERNS` matched in the
    session.jsonl / runner output stream. Concrete pattern is
    attached to the dispatch event for operator debugging."""

    AUTH_EVENT = "auth_event"
    """An ``account_capped`` event surfaced by
    :mod:`_runners._auth_failure`."""


# HTTP-status → RateLimitSignal lookup. Spelled as a dict (not a
# series of ``if status == 429: ...`` branches) so adding a new
# status code is one line — and the classifier reads the dict
# directly via ``in``.
_HTTP_STATUS_MAP: dict[int, RateLimitSignal] = {
    429: RateLimitSignal.HTTP_429,
    403: RateLimitSignal.HTTP_403,
    529: RateLimitSignal.HTTP_529,
}


def classify_http_status(status: int) -> RateLimitSignal | None:
    """Return the :class:`RateLimitSignal` for *status*, or ``None``.

    ``None`` means "this status is not a cap/limit signal" — the
    caller branches on it (network blip, operator bug, server
    error that maps to provider-fallback not rate-limit). Statuses
    OUTSIDE the documented cap set deliberately return ``None`` so
    a future SDK change that adds a 530 doesn't silently get
    treated as a 429.
    """
    return _HTTP_STATUS_MAP.get(int(status))


# Operator-tunable textual patterns. Case-insensitive, ``re.search``
# semantics (matches anywhere in the text). Order is documentation
# only — the scanner returns ``TEXTUAL_MATCH`` on the FIRST hit and
# attaches the concrete pattern string to its return so the operator
# can tell which marker fired.
#
# Adding a pattern: drop a new ``r"..."`` string into the tuple.
# Removing a pattern: delete it. No tests depend on internal order.
DEFAULT_TEXTUAL_PATTERNS: tuple[str, ...] = (
    # Anthropic weekly-cap phrasing observed by operator on an
    # agent account, 2026-06-12 ("You've hit your weekly
    # limit · resets at 2026-06-18T05:00Z").
    r"hit your weekly limit",
    # Generic "weekly limit" / "usage limit" phrasing — covers
    # paraphrases the provider may A/B test.
    r"weekly limit",
    r"usage limit",
    # Common SDK / proxy shape — "You have exceeded your quota".
    r"exceeded your (?:quota|allotted|account) (?:quota|limit|usage)?",
    # OpenAI org-limit phrasing.
    r"organization .*? rate limit",
    # Generic "quota exhausted" / "quota exceeded".
    r"quota (?:exhausted|exceeded|reached)",
)


def scan_textual_cap_markers(
    text: str,
    patterns: Iterable[str] = DEFAULT_TEXTUAL_PATTERNS,
) -> tuple[RateLimitSignal, str] | None:
    """Scan *text* for any cap marker; return (signal, pattern) or ``None``.

    The tuple carries the concrete matching pattern so the
    classifier event payload can record exactly which marker
    fired — invaluable when an operator asks "why did this rotate
    fire" three hours later.

    Empty / whitespace-only input returns ``None`` (no signal,
    not an error). A ``re.error`` from a malformed operator-added
    pattern is silently skipped — the classifier MUST NOT crash
    the runner on a bad pattern; the operator notices via the
    pattern's failure to fire (the standard data-driven feedback
    loop).
    """
    if not text or not text.strip():
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return RateLimitSignal.TEXTUAL_MATCH, pattern
        except re.error:  # stx-allow: fallback (reason: a malformed user pattern must not crash the runner — silent skip preserves the rest of the patterns)
            continue
    return None


__all__ = [
    "DEFAULT_TEXTUAL_PATTERNS",
    "RateLimitSignal",
    "classify_http_status",
    "scan_textual_cap_markers",
]
