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
  ``"You've hit your weekly limit · resets ..."`` (telegrammer
  account, observed 2026-06-12 by operator).
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
* :func:`detect_signal_from_text(text)` — the REACTIVE entry point
  for a stringified SDK failure (``str(exc)`` + captured stderr):
  scans for an embedded HTTP-status signature FIRST
  (:data:`_STATUS_TEXT_SIGNATURES`), then falls back to
  :func:`scan_textual_cap_markers`. This is what closes the gap
  between "the SDK never gives us a typed status code" (see
  ``_runners/_auth_failure.py``'s identical 401 problem) and
  :func:`classify_http_status`, which needs an already-parsed int.
* :data:`DEFAULT_TEXTUAL_PATTERNS` — operator-tunable list of
  case-insensitive regex strings. Driven by data, not code, so
  adding "Xiaomi quota exhausted" or "DeepSeek 402 insufficient
  balance" is a one-line append, not a code review.

Out of scope (deferred):

* Per-provider HTTP error class taxonomy — the dispatcher (PR-B
  ``_runners/_provider_dispatch``) owns 401/402/500-503 etc;
  THIS module concerns itself only with the subset that signals
  ACCOUNT cap (vs network blip, vs operator bug).
* Action layer — Mode A :func:`_account.backoff_agent.backoff_agent`
  and Mode B :func:`_account.rotate_account.rotate_account` are
  sibling modules; :mod:`_runners._rate_limit_reactive` wires this
  file's detectors through :mod:`_account.rate_limit_classifier`
  into those two.
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
    # Anthropic weekly-cap phrasing observed by operator on the
    # telegrammer account, 2026-06-12 ("You've hit your weekly
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
    # PER-MODEL budget, distinct from the subscription windows above.
    # Measured 2026-09-03: a session was stopped by
    #   "You've reached your Fable limit. Run /usage-credits to continue
    #    or switch models with /model."
    # and NONE of the patterns above match it -- it says "limit", not
    # "quota"; "Fable", not "weekly" or "usage". So the cap check fell
    # through and the supervisor's NEXT classifier saw the wreckage, which
    # is auth-shaped: subagents reported "Login expired - Please run
    # /login" while the account was healthy and answering. Catching it here
    # matters because this check runs BEFORE that auth classification, so
    # one data line is the difference between "capped" and an afternoon
    # spent re-authenticating a working credential.
    #
    # Model-agnostic on purpose: the same sentence is issued per model, and
    # tying it to one name guarantees a re-fix on the next one.
    r"reached your \w+ limit",
    # The remedy the same banner offers, as a second, independent marker.
    # A cap detector that depends on one sentence surviving a copy edit is
    # one A/B test away from silence.
    r"/usage-credits",
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


# Signatures that reveal an HTTP status embedded in a STRINGIFIED SDK
# failure — ``str(exc)`` plus whatever stderr
# ``_runners._stderr_capture.enrich_detail_with_stderr`` folded in. The
# ``claude-agent-sdk`` does not surface a typed status code (identical
# gap to the 401 problem ``_runners/_auth_failure.py`` already solves
# with its own narrow substring list), so detection here is text-based
# too.
#
# Provider ``error.type`` strings are checked FIRST — Anthropic's JSON
# error body shape is ``{"type": "error", "error": {"type":
# "rate_limit_error", ...}}`` for 429 and ``"overloaded_error"`` for
# 529; these are unambiguous, provider-defined tokens with no realistic
# false-positive risk. The bare status-code fallback is a
# word-boundary match (``\b429\b`` etc) — narrower than a plain
# substring so an unrelated "...429 lines changed..." in a tool-result
# blob is far less likely to trip it, though — like the 401 list — it
# is not a zero-risk heuristic. Order matters: first match wins.
_STATUS_TEXT_SIGNATURES: tuple[tuple[str, RateLimitSignal], ...] = (
    (r"rate_limit_error", RateLimitSignal.HTTP_429),
    (r"overloaded_error", RateLimitSignal.HTTP_529),
    (r"\b429\b", RateLimitSignal.HTTP_429),
    (r"\b529\b", RateLimitSignal.HTTP_529),
    (r"\b403\b", RateLimitSignal.HTTP_403),
)


def detect_signal_from_text(text: str) -> tuple[RateLimitSignal, str] | None:
    """Detect a REACTIVE rate-limit signal in a stringified SDK failure.

    Two-tier scan, first hit wins:

    1. HTTP-status text signatures (:data:`_STATUS_TEXT_SIGNATURES`) —
       provider ``error.type`` strings, then a bare status-code
       fallback.
    2. Textual cap markers (:func:`scan_textual_cap_markers`) —
       unambiguous cap phrasing such as "hit your weekly limit".

    Parameters
    ----------
    text
        The failure text to scan — typically the exception's ``str()``
        already enriched with captured subprocess stderr (see
        ``_runners._stderr_capture.enrich_detail_with_stderr``).

    Returns
    -------
    tuple[RateLimitSignal, str] | None
        ``(signal, matched_pattern)`` on a hit, else ``None``. Empty /
        whitespace-only input returns ``None``.

    Never raises — a malformed pattern is skipped, same tolerance as
    :func:`scan_textual_cap_markers`.
    """
    if not text or not text.strip():
        return None
    for pattern, signal in _STATUS_TEXT_SIGNATURES:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return signal, pattern
        except re.error:  # stx-allow: fallback (reason: mirrors scan_textual_cap_markers's malformed-pattern tolerance — a bad signature must not crash the runner)
            continue
    return scan_textual_cap_markers(text)


__all__ = [
    "DEFAULT_TEXTUAL_PATTERNS",
    "RateLimitSignal",
    "classify_http_status",
    "detect_signal_from_text",
    "scan_textual_cap_markers",
]
