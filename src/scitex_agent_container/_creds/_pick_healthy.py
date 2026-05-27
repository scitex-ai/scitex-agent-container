"""Pick a healthy stored account at agent-start.

CREDS-PHASE1: an agent pins ``spec.claude.account`` to one of the
operator's saved accounts. When that account's stored credential
snapshot is EXPIRED or ABSENT, today the start fails (see
:class:`scitex_agent_container.runtimes._apptainer_creds.PinnedAccountError`)
and the operator has to manually ``claude /login`` + ``sac accounts
sync-live`` before the agent can run again — even when a *different*
saved account has a perfectly fresh credential.

This picker is the boot-time first pass that rotates around that
manual step: keep the pinned account when it's healthy; otherwise
hand the start a different account whose snapshot IS healthy; raise
loudly when NOTHING is healthy (the genuine "all three accounts
capped/expired" case the operator must fix).

Scope (Phase 1, deliberately small)
-----------------------------------
* Health is *snapshot freshness*: a non-expired ``claudeAiOauth.
  expiresAt`` (the same field :mod:`_account.creds_sync` already
  reads) → ``VALID``. EXPIRED / ABSENT → unhealthy.
* No 5h/7d cap probe — that requires a live Claude API call, is
  not "cheaply detectable", and would itself burn one of the three
  accounts' quota. Cap-induced 429s still surface from claude
  in-turn; the picker only avoids *known-stale* auth at boot.
* Read-only: this module NEVER writes a snapshot. The existing
  per-agent writable boot-copy in
  :func:`runtimes._apptainer_creds.resolve_cred_file` and its
  in-container ~1h token refresh are untouched.
* Pure stdlib; no network call.

Fail-loud contract
------------------
``pick_healthy_account`` raises :class:`NoHealthyAccountError` when
nothing is healthy. The message names every candidate's state so the
operator immediately sees which accounts to refresh. No silent
fallback to a stale snapshot — running a pinned agent on a known-
expired token is exactly what the previous fail-loud guard was added
to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .._account.creds_sync import _read_oauth_expiry_seconds
from .._state.account_store import _store_path

# Health states for one account's snapshot. Mirrors
# :class:`_account.creds_sync.Freshness` but anchored on a *name* so the
# picker can return a structured "why I rotated" record to its caller.
_VALID = "VALID"
_EXPIRED = "EXPIRED"
_ABSENT = "ABSENT"


class NoHealthyAccountError(RuntimeError):
    """Raised when no stored account currently has a usable snapshot.

    The message names every probed account and its state so the
    operator sees the full picture in one error line — they should
    ``claude /login`` to one of them and ``sac accounts sync-live``,
    then restart the agent. Surfaced through ``sac agents start``.
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
        snapshot exists but its ``expiresAt`` is in the past), or
        ``"ABSENT"`` (no snapshot file on disk, or unparseable).
    hours_remaining
        Signed hours to expiry (positive = remaining, negative =
        past) for VALID/EXPIRED; ``None`` for ABSENT.
    """

    name: str
    state: str
    hours_remaining: float | None

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
    expiry = _read_oauth_expiry_seconds(snapshot) if snapshot.is_file() else None
    if expiry is None:
        return AccountHealth(name=name, state=_ABSENT, hours_remaining=None)
    hours = (expiry - now_ts) / 3600.0
    state = _VALID if expiry > now_ts else _EXPIRED
    return AccountHealth(name=name, state=state, hours_remaining=hours)


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


def _format_states(healths: list[AccountHealth]) -> str:
    """Render an operator-facing "name=STATE (+/-Xh)" list for the error."""
    parts: list[str] = []
    for h in healths:
        if h.hours_remaining is None:
            parts.append(f"{h.name}={h.state}")
        else:
            parts.append(f"{h.name}={h.state} ({h.hours_remaining:+.1f}h)")
    return ", ".join(parts) if parts else "(no candidates)"


def pick_healthy_account(
    preferred: str | None,
    *,
    candidates: list[str] | None = None,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> str:
    """Return the stored-account name an agent should run on right now.

    Preference order
    ----------------
    1. ``preferred`` (typically ``spec.claude.account``) when its
       snapshot is :attr:`AccountHealth.is_healthy`.
    2. First healthy candidate, in the order given (when ``candidates``
       is explicit) or sorted alphabetically (when auto-discovered) —
       deterministic so two simultaneous starts pick the same account.

    Parameters
    ----------
    preferred
        The agent's pinned account (``spec.claude.account``). ``None``
        / empty means "no preference" — the picker hands back the first
        healthy candidate instead of raising.
    candidates
        Optional explicit candidate shortlist. ``None`` (default) walks
        every stored account directory on disk. An EMPTY list is
        respected (no auto-discovery fallback) so callers can pin the
        candidate universe in tests.
    store_dir, home, now
        Test overrides. ``store_dir=None`` uses the SciTeX local-state
        cascade (see :func:`_account.account_store._store_path`);
        ``home=None`` uses ``Path.home()``; ``now=None`` uses
        ``time.time()``.

    Returns
    -------
    str
        The picked stored-account name.

    Raises
    ------
    NoHealthyAccountError
        When no candidate is :attr:`AccountHealth.is_healthy`. The
        message names every candidate's state. NEVER falls back to
        a stale snapshot.
    """
    _home = home if home is not None else Path.home()

    if candidates is None:
        cand_list = _discover_candidates(store_dir, _home)
    else:
        cand_list = list(candidates)

    # Make sure `preferred` is considered even when the caller didn't
    # include it in `candidates` — its absent/expired state still
    # belongs in the error message so the operator sees why we rotated.
    pref = (preferred or "").strip()
    if pref and pref not in cand_list:
        cand_list = [pref, *cand_list]

    if not cand_list:
        raise NoHealthyAccountError(
            "no stored accounts to choose from — run "
            "`sac accounts save <name>` or `sac accounts sync-live` "
            "on the credential-holding host, then retry."
        )

    healths = [
        account_health(name, store_dir=store_dir, home=_home, now=now)
        for name in cand_list
    ]
    by_name = {h.name: h for h in healths}

    # 1. Preferred wins when healthy.
    if pref:
        h = by_name.get(pref)
        if h is not None and h.is_healthy:
            return pref

    # 2. First healthy in candidate order.
    for h in healths:
        if h.is_healthy:
            return h.name

    # 3. Nothing healthy — fail loud with full diagnostic.
    raise NoHealthyAccountError(
        "no healthy stored account: "
        f"{_format_states(healths)}. Fix: `claude /login` to one of "
        "them, then `sac accounts sync-live`, then restart the agent."
    )


__all__ = [
    "AccountHealth",
    "NoHealthyAccountError",
    "account_health",
    "pick_healthy_account",
]
