"""Push a freshly-rotated token block into the operator's LIVE ``~/.claude`` login.

This is the REVERSE of :mod:`_account.creds_sync` (which snapshots the
live credential INTO a per-account store). Here, after the refresher
rotates the SNAPSHOT of the account whose ``refreshToken`` matches the
active ``~/.claude`` login, the live file still holds the now-invalidated
(rotated) ``refreshToken`` — the very next live refresh would then 401 and
strand the operator's session. This module copies the three freshly-minted
token fields (``accessToken`` / ``refreshToken`` / ``expiresAt``) from the
rotated snapshot into ``~/.claude/.credentials.json`` so the live session
is never stranded by a rotation.

Only ever call this with a snapshot whose PRE-refresh ``refreshToken``
equalled the live login's ``refreshToken`` (the "active family") — the
caller establishes that equality; this module does not cross accounts.

Safety contract (the live login is IRREPLACEABLE — corruption is
catastrophic, so every write is defended):

1. back up the existing live file to a sibling ``.bak`` before writing;
2. write to a ``.tmp`` then atomically :func:`os.replace` it into place;
3. re-open the written file and VERIFY it parses as JSON and the three
   token fields are present + non-empty; on ANY verification failure,
   RESTORE from the ``.bak`` and raise :class:`ActiveLoginSyncError`.

The existing live JSON structure is preserved byte-for-byte except the
three mutated token fields (mutated inside the ``claudeAiOauth`` block, or
at the document root when the file is stored unwrapped).

Token VALUES are never printed, logged, or returned.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

_TOKEN_FIELDS = ("accessToken", "refreshToken", "expiresAt")


def _default_serialize(data: dict[str, Any]) -> str:
    """Serialize the mutated live document to text (the production writer).

    Kept as a module-level function (not an inline lambda) so it is a
    swappable production boundary: a test can replace this attribute to
    simulate a corrupt write and exercise the verify-or-restore path
    end-to-end through the CLI — the same swap-the-boundary convention the
    suite uses for ``urllib.request.urlopen``.
    """
    return json.dumps(data, indent=2)


class ActiveLoginSyncError(RuntimeError):
    """Raised when the live ``~/.claude`` write fails post-write verification.

    By the time this is raised the original file has already been RESTORED
    from the ``.bak`` backup, so the live login is intact — the exception
    exists to fail the run loudly (non-zero exit) so the operator knows the
    sync did not take, without ever exposing a token value in its message.
    """


def _oauth_block(data: Any) -> dict[str, Any] | None:
    """Return the dict that HOLDS the token fields, or ``None``.

    Prefers the wrapped ``claudeAiOauth`` object (the shape Claude Code
    writes); falls back to the document root when the file is stored
    unwrapped (root already carries ``accessToken``). Never raises.
    """
    if not isinstance(data, dict):
        return None
    inner = data.get("claudeAiOauth")
    if isinstance(inner, dict):
        return inner
    if "accessToken" in data:
        return data
    return None


def read_refresh_token(path: Path) -> str | None:
    """Return the ``refreshToken`` stored at ``path``, or ``None``.

    Reads ``claudeAiOauth.refreshToken`` (or the root ``refreshToken`` for
    an unwrapped file). ``None`` when the file is missing, unparseable, or
    lacks a non-empty refresh token. The value is used ONLY for opaque
    equality comparison by the caller — never printed. Never raises.
    """
    # stx-allow: fallback (reason: a missing / mid-rewrite credential file
    # must read as "no token" (None) so the active-family match simply
    # finds no match, never crashes the refresher.)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    block = _oauth_block(data)
    if block is None:
        return None
    tok = block.get("refreshToken")
    return tok if isinstance(tok, str) and tok.strip() else None


def _read_token_fields(path: Path) -> tuple[str | None, str | None, int | None]:
    """Return ``(accessToken, refreshToken, expiresAt)`` from ``path``.

    ``expiresAt`` is the raw millisecond-epoch integer (or ``None``).
    Never raises.
    """
    # stx-allow: fallback (reason: the freshly-written snapshot is read back
    # to lift its rotated tokens; an unreadable snapshot maps to all-None so
    # the caller aborts the sync loudly instead of crashing.)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None, None, None
    block = _oauth_block(data)
    if block is None:
        return None, None, None
    access = block.get("accessToken")
    refresh = block.get("refreshToken")
    expires = block.get("expiresAt")
    return (
        access if isinstance(access, str) else None,
        refresh if isinstance(refresh, str) else None,
        expires if isinstance(expires, int) and not isinstance(expires, bool) else None,
    )


def _block_is_valid(block: Any) -> bool:
    """True iff ``block`` carries all three token fields, present + non-empty."""
    if not isinstance(block, dict):
        return False
    access = block.get("accessToken")
    refresh = block.get("refreshToken")
    expires = block.get("expiresAt")
    if not (isinstance(access, str) and access.strip()):
        return False
    if not (isinstance(refresh, str) and refresh.strip()):
        return False
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return False
    return expires > 0


def sync_active_login(
    live_path: Path,
    snapshot_path: Path,
    *,
    serialize: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Copy the rotated token block from ``snapshot_path`` into ``live_path``.

    ``live_path`` is the operator's live ``~/.claude/.credentials.json``;
    ``snapshot_path`` is the just-refreshed per-account snapshot whose
    pre-refresh ``refreshToken`` matched the live login (the caller
    establishes that equality). Only the three token fields are mutated;
    every other key in the live file is preserved.

    Follows the backup → atomic-replace → verify-or-restore contract in the
    module docstring. Returns a small status dict (never containing token
    values); raises :class:`ActiveLoginSyncError` when the post-write
    verification fails (after restoring the original from ``.bak``).

    Args:
        live_path: the live ``~/.claude/.credentials.json`` (resolved by
            the caller; symlinks are followed on read/write).
        snapshot_path: the freshly-rotated per-account snapshot.
        serialize: injection seam for tests — serializes the mutated live
            document to text. Defaults to pretty ``json.dumps``. A test can
            pass a serializer that returns malformed / field-stripped text
            to exercise the verify-or-restore path.

    Returns:
        ``{"synced": True, "live_path": <str>}`` on success.

    Raises:
        ActiveLoginSyncError: the snapshot lacked fresh tokens, the live
            file was unreadable, or the post-write verification failed
            (the original is restored from ``.bak`` first).
    """
    live = Path(live_path)
    snap = Path(snapshot_path)
    _serialize = serialize if serialize is not None else _default_serialize

    access, refresh, expires_ms = _read_token_fields(snap)
    if not access or not refresh:
        raise ActiveLoginSyncError(
            f"refusing to sync active login: snapshot {snap!s} lacks a fresh "
            "access/refresh token after refresh"
        )

    if not live.is_file():
        raise ActiveLoginSyncError(
            f"refusing to sync active login: live file not found at {live!s}"
        )

    # Read the ORIGINAL bytes once — this is both the structure we preserve
    # and the exact content we restore on verification failure.
    original_text = live.read_text(encoding="utf-8")
    try:
        live_data = json.loads(original_text)
    except Exception as exc:  # stx-allow: fallback (reason: a corrupt live file is a hard, loud error — never overwrite it blindly)
        raise ActiveLoginSyncError(
            f"refusing to sync active login: live file {live!s} is not valid "
            f"JSON ({exc.__class__.__name__}); leaving it untouched"
        ) from exc

    block = _oauth_block(live_data)
    if block is None:
        raise ActiveLoginSyncError(
            f"refusing to sync active login: live file {live!s} has no "
            "recognisable OAuth token block"
        )

    # Mutate ONLY the three token fields, preserving every other key.
    block["accessToken"] = access
    block["refreshToken"] = refresh
    if expires_ms is not None:
        block["expiresAt"] = expires_ms

    bak = Path(str(live) + ".bak")
    tmp = Path(str(live) + ".tmp")

    # (a) back up the existing file BEFORE writing.
    shutil.copyfile(live, bak)
    # stx-allow: fallback (reason: chmod is best-effort hardening of the
    # backup copy; a filesystem that rejects it must not abort the sync.)
    try:
        os.chmod(bak, 0o600)
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        pass

    # (b) write to .tmp then atomically replace.
    tmp.write_text(_serialize(live_data), encoding="utf-8")
    # stx-allow: fallback (reason: chmod is best-effort hardening; the
    # atomic replace below is what matters for correctness.)
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        pass
    os.replace(tmp, live)

    # (c) verify-or-restore.
    verified = False
    # stx-allow: fallback (reason: ANY failure to re-read/parse the written
    # file counts as a failed verification and triggers the restore below;
    # we never leave a possibly-corrupt live login in place.)
    try:
        written = json.loads(live.read_text(encoding="utf-8"))
        verified = _block_is_valid(_oauth_block(written))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        verified = False

    if not verified:
        # Restore the operator's original login from the backup, then fail
        # loud. The restore is itself atomic (copy to .tmp + replace).
        shutil.copyfile(bak, tmp)
        os.replace(tmp, live)
        raise ActiveLoginSyncError(
            f"active-login write to {live!s} FAILED verification; restored "
            "the original from .bak. The live login was NOT changed."
        )

    return {"synced": True, "live_path": str(live)}


__all__ = ["ActiveLoginSyncError", "read_refresh_token", "sync_active_login"]
