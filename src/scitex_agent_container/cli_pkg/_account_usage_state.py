"""Decide whether a usage figure may be shown as a fact, and say why not.

INCIDENT 2026-08-12 — `sac accounts list` drew a confident 2 % bar for an
account the Anthropic console had at 92 %. A bar is an ASSERTION: a reader
cannot tell a measured 2 % from a 2 % that is a day old, or from a 2 % that
belongs to a different account. Drawing all three identically is what let a
capacity plan be built on a number nobody had checked.

So a usage figure is three-valued here, never two:

``known``   fetched within the fetcher's own refresh window, from an account
            whose identity was verified. Safe to render as a fact.
``stale``   real, attributable, but older than the window in which the
            fetcher itself would have re-fetched it. Renderable WITH its
            age attached — never silently.
``unknown`` sac cannot vouch for it: absent, or belonging to an account
            whose label was not verified or was found wrong. Nothing
            numeric may be drawn.

The staleness threshold is deliberately the FETCHER's TTL rather than a
second, looser display constant. The fetcher treats a cache older than
``_CACHE_TTL_SECONDS`` as needing a re-fetch; if it needed one and did not
get one, the display has no standing to call the value current. Two
thresholds for one fact is how a renderer comes to disagree with the
component it renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .._account.account_verify import MISMATCH, UNVERIFIED, AccountIdentity

# SSOT with the fetcher: a value the fetcher would have re-fetched is, by
# definition, not current. Imported rather than re-declared so the display
# threshold and the fetch threshold can never drift apart.
from .._account.claude_usage import _CACHE_TTL_SECONDS as _FRESH_SECONDS
from ._account_list_format import _coerce_dt

KNOWN = "known"
STALE = "stale"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class UsageReading:
    """A usage figure together with sac's standing to assert it.

    ``pct_5h`` / ``pct_7d`` are ``None`` whenever ``state`` is ``unknown``.
    That is not a convenience — it makes "we don't know" unrepresentable as
    a number, so no downstream renderer or aggregate can accidentally treat
    an unverified figure as data.
    """

    state: str
    pct_5h: float | None = None
    pct_7d: float | None = None
    reset_at_5h: str | None = None
    reset_at_7d: str | None = None
    as_of: str | None = None
    age_seconds: int | None = None
    reason: str | None = None

    @property
    def countable(self) -> bool:
        """May this row contribute to a fleet aggregate?

        Only ``known`` readings count. A ``stale`` figure is shown to the
        operator with its age so they can judge it, but averaging it into a
        fleet capacity number would launder the staleness away — the
        aggregate would look exactly as confident as an all-fresh one.
        """
        return self.state == KNOWN


def _age_seconds(as_of: str | None, now: datetime | None) -> int | None:
    dt = _coerce_dt(as_of)
    if dt is None:
        return None
    _now = now or datetime.now(timezone.utc)
    if _now.tzinfo is None:
        _now = _now.replace(tzinfo=timezone.utc)
    return max(0, int((_now - dt).total_seconds()))


def classify_usage(
    usage: dict | None,
    identity: AccountIdentity | None,
    *,
    now: datetime | None = None,
) -> UsageReading:
    """Grade a raw usage payload into a :class:`UsageReading`.

    Identity is checked BEFORE the numbers, because a figure attributed to
    the wrong account is wrong no matter how fresh it is — that was the
    2026-08-12 failure, where the freshest possible number was also the
    most misleading one.
    """
    if identity is not None:
        # DISOWNED: sac knows these figures are not this row's. Nothing from
        # the snapshot is carried over — not the percentages, not the reset
        # instants, not even the "as of" stamp, because every one of them
        # would describe a different account under this account's name.
        if identity.state == MISMATCH:
            return UsageReading(
                state=UNKNOWN,
                reason=(
                    f"credential belongs to "
                    f"{identity.verified_email or 'another account'}"
                    f", not {identity.claimed_email or identity.name}"
                ),
            )
        if identity.duplicate_of:
            return UsageReading(
                state=UNKNOWN,
                reason=f"same Anthropic account as {identity.duplicate_of}",
            )

    if not usage:
        return UsageReading(state=UNKNOWN, reason="no usage snapshot")
    pct_5h = usage.get("used_pct_5h")
    pct_7d = usage.get("used_pct_7d")
    if pct_5h is None and pct_7d is None:
        return UsageReading(
            state=UNKNOWN, reason=usage.get("error") or "no usage figures returned"
        )

    as_of = usage.get("as_of") or usage.get("fetched_at")
    age = _age_seconds(as_of, now)
    if age is None:
        # A figure with no timestamp cannot be aged, and an un-ageable
        # number is exactly the "fresh-looking but arbitrarily old" shape
        # this module exists to refuse.
        return UsageReading(
            state=UNKNOWN, reason="usage snapshot carries no timestamp"
        )

    state = KNOWN if age < _FRESH_SECONDS else STALE
    reason = None if state == KNOWN else "snapshot older than the refresh window"

    # UNCHECKED: the identity lookup could not run (offline, no token, a
    # credential that changed since it was last verified). That is NOT the
    # same as knowing the figures are somebody else's, so the snapshot's
    # metadata — when the window resets, when the reading was taken — is
    # still carried. Only the PERCENTAGE is withheld, because the percentage
    # is the thing that gets believed.
    #
    # Collapsing this case to a bare `unknown` would blind the whole table on
    # any transient failure of the profile endpoint, which would make the
    # tool useless precisely when an operator reaches for it during an
    # incident. Note a previously-verified account survives an outage anyway:
    # the verifier's cache is bound to the token, so it keeps answering
    # `verified` offline until the credential itself changes.
    if identity is not None and identity.state == UNVERIFIED:
        return UsageReading(
            state=UNKNOWN,
            reset_at_5h=usage.get("reset_at_5h"),
            reset_at_7d=usage.get("reset_at_7d"),
            as_of=as_of,
            age_seconds=age,
            reason="account identity could not be verified",
        )

    return UsageReading(
        state=state,
        pct_5h=pct_5h,
        pct_7d=pct_7d,
        reset_at_5h=usage.get("reset_at_5h"),
        reset_at_7d=usage.get("reset_at_7d"),
        as_of=as_of,
        age_seconds=age,
        reason=reason,
    )


def format_age_short(age_seconds: int | None) -> str:
    """Compact age for the stale marker: ``45s`` / ``12m`` / ``3h`` / ``2d``."""
    if age_seconds is None:
        return "?"
    if age_seconds < 60:
        return f"{age_seconds}s"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h"
    return f"{age_seconds // 86400}d"


__all__ = [
    "KNOWN",
    "STALE",
    "UNKNOWN",
    "UsageReading",
    "classify_usage",
    "format_age_short",
]
