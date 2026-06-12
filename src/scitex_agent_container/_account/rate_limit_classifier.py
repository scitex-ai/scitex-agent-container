"""Rate-limit signal → action-mode classifier (task #13).

Operator directive op-2026-06-12-13 (Telegram 12676 → 12677): the
"is this a transient burst" vs "is the account actually capped"
question MUST be answered by joining the live RateLimitSignal with
the per-account usage% from the quota cache. A single HTTP code
in isolation is not enough — 429 on a low-usage account is
SERVER-side throttle (wait + retry, don't rotate); 429 on a
99%-used account is a hard cap (rotate to a headroom account
NOW).

Public surface:

* :class:`Mode` — closed enum of action decisions: ``NONE`` (no
  rate-limit event), ``BACKOFF`` (Mode A — transient, per-agent
  exponential backoff, same account), ``ROTATE`` (Mode B —
  sustained, account-level rotation).
* :class:`AccountUsageSnapshot` — minimal dataclass the
  classifier consumes. Spelled as its own type (not a raw dict)
  so the classifier's signature stays self-documenting; the
  detector layer projects whatever store shape it reads into this
  shape.
* :func:`classify_rate_limit_signal(signal, usage)` — pure
  function. Given a :class:`RateLimitSignal` and the agent's
  current usage snapshot, return the :class:`Mode` the action
  layer should take.

Action layer — :mod:`_account.backoff_state` (Mode A) and
:mod:`_account.rotate` (Mode B) — consume the classifier's
verdict. The split keeps THIS module pure (no clock, no DB, no
mutation) so its behaviour can be fully tested with literal
input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .rate_limit_signals import RateLimitSignal

# ---------------------------------------------------------------------------
# Thresholds (operator-locked TG 12674)
# ---------------------------------------------------------------------------

# Proactive 5h-window utilisation threshold for Mode B (ROTATE).
# Operator-locked: 95% warn, 99% trigger. We expose only the
# trigger here — the warn is the detector daemon's logging
# threshold, not the classifier's action threshold.
_ROTATE_PCT_5H = 99.0

# Proactive 7d-window utilisation threshold for Mode B (ROTATE).
_ROTATE_PCT_7D = 95.0

# 5h-window utilisation BELOW which a one-shot 429/529 is
# classified as transient (Mode A). At/above this, even a single
# 429 is taken as cap-class. Per operator: 90% / 85% on 5h / 7d.
_TRANSIENT_PCT_5H = 90.0
_TRANSIENT_PCT_7D = 85.0


class Mode(str, Enum):
    """Action decision the classifier hands to the dispatcher.

    str-enum so the value serialises cleanly into
    ``account_rotated`` / ``account_backoff`` events (alongside
    :class:`RateLimitSignal`) without a custom encoder.
    """

    NONE = "none"
    """The signal is not a rate-limit/cap event the action layer
    should respond to. The runner continues unchanged. The
    classifier returns NONE for AUTH_EVENT and TEXTUAL_MATCH only
    when the usage% is verifiably healthy AND the signal is the
    weakest evidence (purely transient phrasing); in practice
    NONE is the "ignore" branch."""

    BACKOFF = "backoff"
    """Mode A — TRANSIENT burst rate-limit (server-side throttle,
    account has headroom). Per-agent exponential backoff, SAME
    account. The sibling protection rule (one agent's backoff
    must not starve other agents on the same account) is the
    action layer's responsibility; the classifier just emits
    the decision."""

    ROTATE = "rotate"
    """Mode B — SUSTAINED account cap. Rotate the agent to a
    headroom account and restart per the operator's "never park,
    always move work" doctrine."""


@dataclass(frozen=True)
class AccountUsageSnapshot:
    """The classifier's view of one account's current usage.

    Frozen because the classifier is pure — mutating a snapshot
    mid-classify is always a code smell.

    Fields use the same names the quota cache + a2a metadata
    already standardised on (``used_pct_5h`` / ``used_pct_7d``),
    so the detector layer's projection is field-rename-free.
    """

    used_pct_5h: float = 0.0
    used_pct_7d: float = 0.0


def classify_rate_limit_signal(
    signal: RateLimitSignal,
    usage: AccountUsageSnapshot,
) -> Mode:
    """Return the :class:`Mode` the action layer should take.

    Decision matrix (operator-locked TG 12676/12677):

    * ``USAGE_PCT_5H`` proactive signal (the detector saw the
      cache crossing the rotate threshold):
        ``used_pct_5h >= 99`` → ROTATE
        else                  → NONE (warn-only, not actioned)
    * ``USAGE_PCT_7D`` proactive signal:
        ``used_pct_7d >= 95`` → ROTATE
        else                  → NONE
    * ``HTTP_429`` / ``HTTP_403`` reactive: ROTATE iff the
      account is ALREADY in cap territory (5h >= 90% OR 7d >=
      85%); else BACKOFF (transient burst, headroom remains).
    * ``HTTP_529`` (Anthropic overload): always BACKOFF — 529 is
      explicitly defined as server-capacity throttle, not account
      cap. Even at high usage% we wait it out (per provider
      docs, retrying on 529 is the documented path).
    * ``TEXTUAL_MATCH``: ALWAYS ROTATE. The textual markers
      (``"hit your weekly limit"`` / ``"quota exceeded"`` etc)
      are unambiguous cap signals from the provider — they don't
      fire for transient throttle.
    * ``AUTH_EVENT``: ALWAYS ROTATE. The
      :mod:`_runners._auth_failure` hook only emits
      ``account_capped`` after its own confirmation pass; we
      trust it.

    Pure: no clock, no DB, no logging, no side effects. The
    action layer owns the consecutive-hit accounting (5 backoffs
    in a row → escalate to ROTATE — that lives on the backoff
    state machine, not here).
    """
    if signal is RateLimitSignal.USAGE_PCT_5H:
        return Mode.ROTATE if usage.used_pct_5h >= _ROTATE_PCT_5H else Mode.NONE
    if signal is RateLimitSignal.USAGE_PCT_7D:
        return Mode.ROTATE if usage.used_pct_7d >= _ROTATE_PCT_7D else Mode.NONE
    if signal is RateLimitSignal.HTTP_529:
        return Mode.BACKOFF
    if signal in (RateLimitSignal.HTTP_429, RateLimitSignal.HTTP_403):
        if (
            usage.used_pct_5h >= _TRANSIENT_PCT_5H
            or usage.used_pct_7d >= _TRANSIENT_PCT_7D
        ):
            return Mode.ROTATE
        return Mode.BACKOFF
    if signal in (RateLimitSignal.TEXTUAL_MATCH, RateLimitSignal.AUTH_EVENT):
        return Mode.ROTATE
    return Mode.NONE


__all__ = [
    "AccountUsageSnapshot",
    "Mode",
    "classify_rate_limit_signal",
]
