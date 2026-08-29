"""Account-snapshot health probing + operator-facing diagnostics.

The health-model layer beneath :func:`._pick_healthy.pick_healthy_account`.
Where :mod:`._pick_healthy` answers "which fresh account should run now?",
this module answers the prior question for ONE account — "is its stored
credential snapshot usable?" — and renders the fail-loud error message when
nothing is.

Split out of ``_pick_healthy`` (constitution §3: one cohesive responsibility
per file) so the picker file stays the pure decision. ``_pick_healthy``
re-imports every public name here, so
``from scitex_agent_container._creds._pick_healthy import account_health`` (used
by :mod:`_account.mint_token`, :mod:`_account.quota_watch`, and tests) keeps
resolving unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .._account.creds_sync import (
    _oauth_expiry_to_seconds,
    _read_oauth_expiry_raw,
)
from .._state.account_store import _store_path
from ._entitlement import read_entitlement
from ._pause import read_pause

# Health states for one account's snapshot. Mirrors
# :class:`_account.creds_sync.Freshness` but anchored on a *name* so the
# picker can return a structured "why I rotated" record to its caller.
_VALID = "VALID"
_EXPIRED = "EXPIRED"
_ABSENT = "ABSENT"
#: The token is FRESH but the account may not USE it. INCIDENT
#: 2026-08-25: a cancelled subscription refreshes its OAuth token
#: perfectly well, so it read VALID here while every real turn on it
#: returned 403 "OAuth authentication is currently not allowed for this
#: organization". Freshness and entitlement are different questions;
#: this state is the second one. Read from a cached verdict written
#: out-of-band by the host timer -- see :mod:`._entitlement` for why it
#: must not be probed live at boot, and why UNKNOWN never lands here.
_FORBIDDEN = "FORBIDDEN"
#: The operator has DECIDED to stop using this account for a while.
#: OPERATOR REQUEST 2026-08-26: he stops and restarts subscriptions
#: while watching quota, and asked that nothing fail during the rest.
#: Read from an operator-authored sidecar -- see :mod:`._pause` for why
#: a decision must not share a file with an observation, and why this
#: state outranks every measured one below.
_PAUSED = "PAUSED"


class NoHealthyAccountError(RuntimeError):
    """Raised when no stored account currently has a usable snapshot.

    ``str(exc)`` is the full diagnosis (every probed account, its state,
    and the evidence path); :attr:`brief` is the single line the CLI
    shows by default — what is wrong and the one command that fixes it.
    An expected, deliberately-raised condition: callers render
    :attr:`brief` instead of letting it escape as a traceback.
    """

    def __init__(self, message: str, *, brief: str = "") -> None:
        super().__init__(message)
        self.brief = brief or message


class BlindQuotaCacheError(NoHealthyAccountError):
    """The pick is unconfirmable because the quota cache told us NOTHING.

    A SUBCLASS so callers can discriminate by TYPE rather than by matching
    the message text. The distinction is operational, not cosmetic: this is
    the ONE failure in the family that a caller can often repair by itself —
    refreshing the cache and re-picking — whereas every other
    ``NoHealthyAccountError`` (no fresh candidate, all accounts expired)
    needs a human to log in. Callers that retry must retry ONLY this one.
    """


@dataclass(frozen=True)
class AccountHealth:
    """Health of one stored account's credential snapshot.

    Attributes
    ----------
    name
        The stored-account name (a slug — see
        :func:`_account.creds_sync.slugify_email`).
    state
        ``"VALID"`` (non-expired snapshot present), ``"EXPIRED"`` (a
        snapshot exists but its ``expiresAt`` is in the past),
        ``"ABSENT"`` (no snapshot file on disk, or unparseable),
        ``"FORBIDDEN"`` (fresh, but a measured 403 says this account may
        not use Claude Code), or ``"PAUSED"`` (the operator decided to
        rest it). The last two are different KINDS of answer -- one
        measured, one authored -- which is why they are separate states
        and never collapse into each other.
    hours_remaining
        Signed hours to expiry (positive = remaining, negative =
        past) for VALID/EXPIRED; ``None`` for ABSENT.
    snapshot_path
        The EXACT file this probe read (or looked for). INCIDENT
        2026-07-10: the "EXPIRED (-5.8h)" boot error hid which file it
        had evaluated, so a snapshot repaired minutes later looked like
        a false read and cost the investigation hours. Every health
        record now carries its evidence.
    expires_at_raw
        The literal ``claudeAiOauth.expiresAt`` value found in the file
        (claude-code writes unix milliseconds); ``None`` for ABSENT.
    """

    name: str
    state: str
    hours_remaining: float | None
    snapshot_path: str | None = None
    expires_at_raw: float | None = None
    #: For a FORBIDDEN record, the API's own words. An operator seeing
    #: "your token is fine but you cannot use it" needs to be told why
    #: in the same breath, or the state looks like our bug.
    entitlement_detail: str = ""
    #: For a PAUSED record, the operator's OWN words for why he stopped
    #: it. A SEPARATE field from ``entitlement_detail`` on purpose: one
    #: carries what Anthropic said about us, the other carries what we
    #: decided about Anthropic, and a single field would make a probe's
    #: sentence and a human's sentence indistinguishable at the point of
    #: reading. See :mod:`._pause`.
    pause_reason: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.state == _VALID


def account_health(
    name: str,
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> AccountHealth:
    """Return the health of one stored account's snapshot.

    Never raises. A missing / unparseable snapshot reads as
    :class:`AccountHealth` ``state="ABSENT"``.

    This is functionally equivalent to
    :func:`_account.creds_sync.account_freshness`, re-exposed under a
    name-anchored shape so :func:`pick_healthy_account` can build the
    diagnostic error message without losing the account name.
    """
    _home = home if home is not None else Path.home()
    now_ts = now if now is not None else time.time()

    store = _store_path(store_dir, _home)
    snapshot = store / name / ".credentials.json"
    # ONE read supplies BOTH the raw evidence value and the normalised
    # expiry, so the record can never quote a different file state than
    # the one it judged (a mid-probe rewrite yields a coherent record).
    raw = _read_oauth_expiry_raw(snapshot) if snapshot.is_file() else None

    # A PAUSE OUTRANKS EVERY MEASUREMENT, and is asked FIRST -- before
    # the ABSENT return below and before the entitlement read further
    # down. The rule this extends is already written for the layer
    # underneath ("overwriting that with FORBIDDEN would hide the
    # actionable fault behind a second one"): report the fact the
    # operator can ACT on. When he has decided to rest an account,
    # "FORBIDDEN" or "ABSENT" is a true sentence about a question nobody
    # is asking, and rendering it would make a deliberate rest look like
    # a fault -- the exact confusion :mod:`._pause` exists to prevent.
    # A local file read; no network.
    pause = read_pause(name, store / name)
    if pause.active:
        return AccountHealth(
            name=name,
            state=_PAUSED,
            # The evidence fields are still filled in from whatever the
            # snapshot read found, so a paused account's record can
            # still be adjudicated without un-pausing it first.
            hours_remaining=(
                None
                if raw is None
                else (_oauth_expiry_to_seconds(raw) - now_ts) / 3600.0
            ),
            snapshot_path=str(snapshot),
            expires_at_raw=raw,
            pause_reason=pause.reason,
        )

    if raw is None:
        return AccountHealth(
            name=name,
            state=_ABSENT,
            hours_remaining=None,
            snapshot_path=str(snapshot),
            expires_at_raw=None,
        )
    expiry = _oauth_expiry_to_seconds(raw)
    hours = (expiry - now_ts) / 3600.0
    state = _VALID if expiry > now_ts else _EXPIRED

    # ENTITLEMENT is asked only of an otherwise-usable snapshot. An
    # EXPIRED token's problem is already named, and overwriting that
    # with FORBIDDEN would hide the actionable fault behind a second
    # one. A local file read; no network. Only a MEASURED denial
    # downgrades the state -- UNKNOWN leaves it exactly as freshness
    # found it, per the constitution's three-valued rule.
    if state == _VALID:
        verdict = read_entitlement(name, store / name, now=now_ts)
        if verdict.blocks_use:
            return AccountHealth(
                name=name,
                state=_FORBIDDEN,
                hours_remaining=hours,
                snapshot_path=str(snapshot),
                expires_at_raw=raw,
                entitlement_detail=verdict.detail,
            )

    return AccountHealth(
        name=name,
        state=state,
        hours_remaining=hours,
        snapshot_path=str(snapshot),
        expires_at_raw=raw,
    )


def _discover_candidates(
    store_dir: Path | None,
    home: Path,
) -> list[str]:
    """Return every stored-account name on disk (sorted, deterministic).

    Best-effort: a missing / unreadable store reads as no candidates
    (the caller turns that into :class:`NoHealthyAccountError`). We
    skip the store-internal ``_rotations/`` housekeeping dir so it
    can never get picked.
    """
    store = _store_path(store_dir, home)
    if not store.is_dir():
        return []
    out: list[str] = []
    # stx-allow: fallback (reason: store iteration is best-effort
    # discovery; an unreadable dir / partially-created entry must
    # degrade to "no candidates" so the caller's fail-loud takes over,
    # never crash with an OSError mid-resolution.)
    try:
        for child in sorted(store.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith("_"):
                continue
            out.append(child.name)
    except OSError:
        return []
    return out


def _iso_utc(seconds: float) -> str:
    """Render a unix-seconds timestamp as a compact ISO-8601 UTC string."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _format_states(healths: list[AccountHealth]) -> str:
    """Render the operator-facing per-account evidence list for the error.

    Self-diagnosing (INCIDENT 2026-07-10): each entry names the file
    that was read, the RAW ``expiresAt`` value found in it, and its UTC
    rendering — so a "this account was fine minutes later!" report can
    be adjudicated from the error line alone (the snapshot may simply
    have been rewritten between the probe and the re-check).
    """
    parts: list[str] = []
    for h in healths:
        if h.hours_remaining is None or h.expires_at_raw is None:
            base = (
                f"{h.name}={h.state} (no numeric claudeAiOauth.expiresAt; "
                f"file={h.snapshot_path})"
            )
        else:
            expiry_s = _oauth_expiry_to_seconds(h.expires_at_raw)
            base = (
                f"{h.name}={h.state} ({h.hours_remaining:+.1f}h; "
                f"expiresAt={h.expires_at_raw:.0f} = {_iso_utc(expiry_s)}; "
                f"file={h.snapshot_path})"
            )
        # A boot blocked BY A PAUSE must name WHICH pause, in the first
        # error its operator reads. Without this the message says the
        # account is unusable and offers `claude /login` — advice that
        # cannot work, for a condition one `sac accounts resume` lifts.
        if h.pause_reason:
            base += f"; paused: {h.pause_reason}"
        parts.append(base)
    return ", ".join(parts) if parts else "(no candidates)"


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_health",
]
