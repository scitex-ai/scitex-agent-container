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

Pure stdlib. No network call. The store-name is the account email
slugified (``wyusuuke@gmail.com`` → ``wyusuuke-gmail-com``), matching
the layout the rest of sac already uses on disk.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


class LiveCredInvalidError(RuntimeError):
    """Raised when the live credential is absent, malformed, or expired.

    The message names the live path and tells the operator how to fix
    it (``claude /login``). Surfaced as a non-zero CLI exit so the
    operator never mistakes a stale token for a successful sync.
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


def sync_live(
    home: Path | None = None,
    store_dir: Path | None = None,
    *,
    now: float | None = None,
) -> SyncResult:
    """Mirror the live credential into its matching store when warranted.

    Reads the live ``~/.claude/.credentials.json`` and the active email
    from ``~/.claude.json``. The live cred must be VALID (``expiresAt``
    strictly in the future) — otherwise :class:`LiveCredInvalidError`.
    Derives the store-name from the email; if the store is ABSENT, OR
    its snapshot is OLDER (earlier ``expiresAt``) than the live cred,
    OR the store snapshot is itself expired, the live cred is
    atomically snapshotted in and the account metadata refreshed.

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
    """
    _home = home or Path.home()
    now_ts = now if now is not None else time.time()

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

    email = _read_active_email(_home)
    if email is None:
        raise LiveCredInvalidError(
            f"cannot determine the active account email from "
            f"{(_home / '.claude.json')!s} (missing "
            "`oauthAccount.emailAddress`). Run `claude /login`."
        )

    store_name = slugify_email(email)

    from .._state.account_store import _store_path, save_account

    store = _store_path(store_dir, _home)
    snapshot = store / store_name / ".credentials.json"
    store_expiry = _read_oauth_expiry_seconds(snapshot) if snapshot.is_file() else None

    # Idempotent: store already matches or is fresher than live → no-op.
    if store_expiry is not None and store_expiry >= live_expiry:
        return SyncResult(
            action="up-to-date",
            store_name=store_name,
            email=email,
            live_expires_at=live_expiry,
            store_expires_at=store_expiry,
        )

    _atomic_copy(live_path, snapshot)
    # Refresh the safe metadata (email label) so `account list` resolves
    # the email even for a store created purely by auto-sync.
    save_account(store_name, {"email_address": email}, store_dir=store_dir, home=_home)

    return SyncResult(
        action="saved",
        store_name=store_name,
        email=email,
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
    "Freshness",
    "LiveCredInvalidError",
    "SyncResult",
    "account_freshness",
    "slugify_email",
    "sync_live",
]
