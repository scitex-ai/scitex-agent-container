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

# Health states for one account's snapshot. Mirrors
# :class:`_account.creds_sync.Freshness` but anchored on a *name* so the
# picker can return a structured "why I rotated" record to its caller.
_VALID = "VALID"
_EXPIRED = "EXPIRED"
_ABSENT = "ABSENT"


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
        snapshot exists but its ``expiresAt`` is in the past), or
        ``"ABSENT"`` (no snapshot file on disk, or unparseable).
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
            parts.append(
                f"{h.name}={h.state} (no numeric claudeAiOauth.expiresAt; "
                f"file={h.snapshot_path})"
            )
        else:
            expiry_s = _oauth_expiry_to_seconds(h.expires_at_raw)
            parts.append(
                f"{h.name}={h.state} ({h.hours_remaining:+.1f}h; "
                f"expiresAt={h.expires_at_raw:.0f} = {_iso_utc(expiry_s)}; "
                f"file={h.snapshot_path})"
            )
    return ", ".join(parts) if parts else "(no candidates)"


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_health",
]
