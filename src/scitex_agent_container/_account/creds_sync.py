"""Credential auto-sync engine: mirror the live Claude OAuth into the store.

The moment the operator runs ``claude /login`` to an account, the live
``~/.claude/.credentials.json`` is replaced with that account's fresh
OAuth bundle. Nothing re-saves it into the sac per-account store
(``~/.scitex/agent-container/accounts/<slug>/.credentials.json``), so
stores rot — the operator must remember a manual ``sac accounts save``.

This module is the one-shot engine behind ``sac accounts sync-live``:
it reads the live credential + the active account's email, and — when
the live cred is VALID and newer/fresher than the matching store —
atomically snapshots it into that store. It is idempotent: a store
that already matches or is newer than the live cred is a no-op.

Fail-loud contract (no silent fallbacks): an EXPIRED or ABSENT live
credential is a hard error (:class:`LiveCredInvalidError`), never a
silent save of a stale token.

The store-name is the account email slugified (``wyusuuke@gmail.com`` →
``wyusuuke-gmail-com``), matching the layout the rest of sac already uses
on disk.

Identity guard (2026-07)
------------------------
The TARGET store is derived from the LIVE TOKEN's ACTUAL identity — a
best-effort OAuth "whoami" (:func:`_account.account_identity.fetch_account_email`)
— NOT from ``~/.claude.json``'s ``oauthAccount.emailAddress`` metadata,
which drifts out of sync with the token across logins/switches. That drift
caused ``sync_live`` to snapshot one account's token into ANOTHER account's
store (clobbering it) on 2026-07. A single best-effort network call is made
per sync; when it FAILS (offline), the code falls back to the metadata email
BUT refuses any write that would change a store's recorded identity. A
store's credential must always authenticate as that store's own account.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class LiveCredInvalidError(RuntimeError):
    """Raised when the live credential is absent, malformed, or expired.

    The message names the live path and tells the operator how to fix
    it (``claude /login``). Surfaced as a non-zero CLI exit so the
    operator never mistakes a stale token for a successful sync.
    """


class AccountIdentityError(LiveCredInvalidError):
    """Raised when a sync would change a store's recorded account identity.

    Subclasses :class:`LiveCredInvalidError` so the ``watch-live`` loop and
    the ``sync-live`` CLI already treat it as a soft, LOGGED failure — the
    write is refused (never corrupt a store on identity uncertainty), never
    crashed. This is the offline-path safety net for the 2026-07
    credential-clobber bug: when the live token's identity cannot be
    verified (whoami offline) AND the target store already records a
    different account, we abort rather than overwrite.
    """


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one :func:`sync_live` run.

    Attributes
    ----------
    action
        One of ``"saved"`` (store written), ``"up-to-date"`` (store
        already matched/newer — no write).
    store_name
        Slugified store directory name the live cred maps to.
    email
        Active-account email read from ``~/.claude.json``.
    live_expires_at
        Live credential expiry, unix seconds.
    store_expires_at
        Prior store expiry (unix seconds), or ``None`` when the store
        was absent / unreadable before this run.
    """

    action: str
    store_name: str
    email: str
    live_expires_at: float
    store_expires_at: float | None


def slugify_email(email: str) -> str:
    """Map an account email to its on-disk store-name.

    ``@`` and ``.`` collapse to ``-`` and the result is lower-cased,
    matching the existing layout (``wyusuuke@gmail.com`` →
    ``wyusuuke-gmail-com``, ``ywatanabe@scitex.ai`` →
    ``ywatanabe-scitex-ai``).
    """
    return email.strip().lower().replace("@", "-").replace(".", "-")


def _read_oauth_expiry_seconds(path: Path) -> float | None:
    """Return the OAuth ``expiresAt`` of ``path`` in unix seconds, or None.

    ``None`` when the file is missing, unparseable, or lacks a numeric
    ``claudeAiOauth.expiresAt``. claude-code writes ``expiresAt`` as a
    unix-MILLISECOND integer; any value above ``1e12`` is treated as
    milliseconds and divided by 1000 (matching ``_preflight_creds``).
    Never raises.
    """
    # stx-allow: fallback (reason: store-snapshot freshness probe is
    # best-effort; a missing/corrupt snapshot must read as "no expiry"
    # (None) so callers treat it as stale/absent, never crash the sync.)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    raw = oauth.get("expiresAt")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    val = float(raw)
    return val / 1000.0 if val > 1e12 else val


def _read_active_email(home: Path) -> str | None:
    """Return the active-account email from ``~/.claude.json``, or None.

    Reads ``oauthAccount.emailAddress``. ``None`` when the file is
    missing, unparseable, or lacks the field. Never raises.
    """
    # stx-allow: fallback (reason: ~/.claude.json may be absent or
    # mid-rewrite by claude-code; a missing email maps to None so the
    # caller raises a clear "cannot determine account" error rather than
    # crashing on a transient read.)
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` atomically (tmp + rename, preserves mode)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dst)


def _read_store_email(account_dir: Path) -> str | None:
    """Return the ``email_address`` recorded in a store's ``account.json``.

    ``None`` when the metadata file is missing, unparseable, or lacks a
    non-empty ``email_address``. Never raises. Used by the offline safety
    guard to detect that a target store already belongs to a different
    account before overwriting its credential.
    """
    # stx-allow: fallback (reason: a store's account.json may be absent or
    # mid-rewrite; a missing/corrupt identity reads as None so the offline
    # guard treats it as "no recorded identity" rather than crashing.)
    try:
        data = json.loads((account_dir / "account.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    email = data.get("email_address")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def _default_identity_fn(live_path: Path) -> str | None:
    """Best-effort whoami for the live credential (lazy import, offline-safe).

    Imported lazily so importing this module never triggers the network
    dependency, and so ``sync_live`` makes the OAuth call only when actually
    run. Returns ``None`` on any failure — the caller falls back to the
    metadata email under the offline guard.
    """
    from .account_identity import fetch_account_email

    return fetch_account_email(live_path)


def sync_live(
    home: Path | None = None,
    store_dir: Path | None = None,
    *,
    now: float | None = None,
    identity_fn: Callable[[Path], str | None] | None = None,
) -> SyncResult:
    """Mirror the live credential into its matching store when warranted.

    Reads the live ``~/.claude/.credentials.json``. The live cred must be
    VALID (``expiresAt`` strictly in the future) — otherwise
    :class:`LiveCredInvalidError`.

    Derives the target store from the live TOKEN's ACTUAL identity via a
    best-effort whoami (``identity_fn``), NOT from the possibly-stale
    ``~/.claude.json`` metadata email — this is the fix for the 2026-07
    wrong-store clobber. When the whoami is offline it falls back to the
    metadata email but refuses any write that would change a store's
    recorded identity (:class:`AccountIdentityError`).

    If the target store is ABSENT, OR its snapshot is OLDER (earlier
    ``expiresAt``) than the live cred, OR the store snapshot is itself
    expired, the live cred is atomically snapshotted in and the account
    metadata refreshed.

    Idempotent: when the store snapshot already has an ``expiresAt`` >=
    the live cred's, returns ``action="up-to-date"`` without writing.

    Parameters
    ----------
    home
        Override for the user home (tests pass ``tmp_path``). Defaults
        to ``Path.home()``.
    store_dir
        Override for the accounts store root. Defaults to the
        ``account_store`` cascade.
    now
        Override for the wall clock (unix seconds). Defaults to
        ``time.time()``.
    identity_fn
        Best-effort whoami: ``live_path -> account_email | None``. Injected
        in tests; defaults to :func:`_default_identity_fn` (an OAuth profile
        call). Returns ``None`` when the identity cannot be verified
        (offline), triggering the metadata-based fallback + safety guard.

    Returns
    -------
    SyncResult
        Describing what happened (``saved`` / ``up-to-date``).

    Raises
    ------
    LiveCredInvalidError
        When the live credential is absent, malformed, expired, or the
        active email cannot be determined. The message tells the
        operator to ``claude /login``.
    AccountIdentityError
        (Subclass of ``LiveCredInvalidError``.) When the live token's
        identity could not be verified (offline) AND the target store
        already records a DIFFERENT account — the write is refused rather
        than clobber that store's credential.
    """
    _home = home or Path.home()
    now_ts = now if now is not None else time.time()
    _identity_fn = identity_fn if identity_fn is not None else _default_identity_fn

    live_path = _home / ".claude" / ".credentials.json"
    if not live_path.is_file():
        raise LiveCredInvalidError(
            f"live credential not found at {live_path!s}. "
            "Run `claude /login` to create it."
        )

    live_expiry = _read_oauth_expiry_seconds(live_path)
    if live_expiry is None:
        raise LiveCredInvalidError(
            f"live credential at {live_path!s} is missing a numeric "
            "`claudeAiOauth.expiresAt`. Run `claude /login` to regenerate it."
        )
    if live_expiry <= now_ts:
        ago = int(now_ts - live_expiry)
        raise LiveCredInvalidError(
            f"live credential at {live_path!s} expired {ago} seconds ago; "
            "refusing to sync a stale token. Run `claude /login` to refresh."
        )

    metadata_email = _read_active_email(_home)

    # --- Identity guard: derive the target store from the TOKEN, not the
    #     (possibly stale) ~/.claude.json metadata. ------------------------
    # stx-allow: fallback (reason: whoami is best-effort; a None return means
    # the token identity is unverifiable (offline) and we drop to the guarded
    # metadata fallback below — never crash the sync on a network hiccup.)
    token_email = _identity_fn(live_path)

    if token_email is not None:
        # Trust the live token's actual identity. If the metadata disagrees,
        # it is stale — warn and write to the TOKEN-identity store anyway
        # (this is the fix for the 2026-07 wrong-store clobber).
        if metadata_email and metadata_email.lower() != token_email.lower():
            logger.warning(
                "sync-live: ~/.claude.json email %r disagrees with the live "
                "token's actual account %r; trusting the token and syncing "
                "into its store (metadata is stale).",
                metadata_email,
                token_email,
            )
        target_email = token_email
        identity_verified = True
    else:
        # Offline / whoami failed — fall back to metadata selection under the
        # safety guard below.
        if metadata_email is None:
            raise LiveCredInvalidError(
                f"cannot determine the active account email from "
                f"{(_home / '.claude.json')!s} (missing "
                "`oauthAccount.emailAddress`) and the live token's identity "
                "could not be verified. Run `claude /login`."
            )
        target_email = metadata_email
        identity_verified = False

    store_name = slugify_email(target_email)

    from .._state.account_store import _store_path, save_account

    store = _store_path(store_dir, _home)
    account_dir = store / store_name
    snapshot = account_dir / ".credentials.json"
    store_expiry = _read_oauth_expiry_seconds(snapshot) if snapshot.is_file() else None

    # Idempotent: store already matches or is fresher than live → no-op.
    if store_expiry is not None and store_expiry >= live_expiry:
        return SyncResult(
            action="up-to-date",
            store_name=store_name,
            email=target_email,
            live_expires_at=live_expiry,
            store_expires_at=store_expiry,
        )

    # Offline safety guard: when the token identity is UNVERIFIED, never
    # overwrite a store whose recorded account differs from where the
    # metadata points — that is the exact shape of the 2026-07 clobber.
    if not identity_verified:
        recorded = _read_store_email(account_dir)
        if recorded is not None and recorded.lower() != target_email.lower():
            raise AccountIdentityError(
                f"refusing to sync into store {store_name!r}: it already "
                f"records account {recorded!r}, which differs from the "
                f"metadata email {target_email!r}, and the live token's "
                "identity could not be verified (offline). Refusing to change "
                "a store's identity. Run `claude /login` or restore network "
                "so the token can be verified."
            )

    _atomic_copy(live_path, snapshot)
    # Refresh the safe metadata (email label) so `account list` resolves
    # the email even for a store created purely by auto-sync.
    save_account(
        store_name, {"email_address": target_email}, store_dir=store_dir, home=_home
    )

    return SyncResult(
        action="saved",
        store_name=store_name,
        email=target_email,
        live_expires_at=live_expiry,
        store_expires_at=store_expiry,
    )


@dataclass(frozen=True)
class Freshness:
    """Freshness of a single stored account's credential snapshot.

    Attributes
    ----------
    state
        ``"VALID"``, ``"EXPIRED"``, or ``"ABSENT"``.
    hours
        Signed hours to expiry (positive = remaining, negative = past)
        for VALID/EXPIRED; ``None`` for ABSENT.
    """

    state: str
    hours: float | None

    def label(self) -> str:
        """Render the column cell: ``VALID (+5.7h)`` / ``EXPIRED (-138.6h)`` / ``ABSENT``."""
        if self.state == "ABSENT" or self.hours is None:
            return "ABSENT"
        return f"{self.state} ({self.hours:+.1f}h)"


def account_freshness(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
    *,
    now: float | None = None,
) -> Freshness:
    """Return the credential freshness of stored account ``name``.

    Reads ``<store>/<name>/.credentials.json`` and compares its
    ``expiresAt`` to ``now``. Never raises — an absent/corrupt snapshot
    reads as :class:`Freshness` ``ABSENT``.
    """
    _home = home or Path.home()
    now_ts = now if now is not None else time.time()

    from .._state.account_store import _store_path

    store = _store_path(store_dir, _home)
    snapshot = store / name / ".credentials.json"
    expiry = _read_oauth_expiry_seconds(snapshot) if snapshot.is_file() else None
    if expiry is None:
        return Freshness(state="ABSENT", hours=None)
    hours = (expiry - now_ts) / 3600.0
    state = "VALID" if expiry > now_ts else "EXPIRED"
    return Freshness(state=state, hours=hours)


__all__ = [
    "AccountIdentityError",
    "Freshness",
    "LiveCredInvalidError",
    "SyncResult",
    "account_freshness",
    "slugify_email",
    "sync_live",
]
