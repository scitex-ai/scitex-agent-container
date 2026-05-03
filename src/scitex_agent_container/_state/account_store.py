"""Credential account store for multi-account rotation.

Manages a directory of saved Claude Code accounts.  Each account is stored
as a JSON file in ``~/.scitex/sac/accounts/<name>.json`` containing only
safe metadata (no tokens).  The credentials files themselves live in a
per-account snapshot directory and are swapped into place by
``switch_account``.

Design rules
------------
1. No token material is ever stored in the account metadata JSON.
2. ``list_accounts()`` never raises.
3. ``switch_account()`` copies credential files atomically.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

_DEFAULT_STORE_SUBDIR = Path(".scitex") / "sac" / "accounts"


def _store_path(store_dir: Path | None, home: Path) -> Path:
    if store_dir is not None:
        return Path(store_dir)
    return home / _DEFAULT_STORE_SUBDIR


def list_accounts(
    store_dir: Path | None = None,
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """Return list of saved account metadata dicts.

    Each dict has at least ``name`` (str).  Optional fields include
    ``email_address`` and ``quota_5h_used_pct`` (float or None).

    Never raises.  Returns empty list if the store directory does not
    exist or is unreadable.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    accounts: list[dict[str, Any]] = []
    if not store.is_dir():
        return accounts
    for meta_file in sorted(store.glob("*.json")):
        # stx-allow: fallback (reason: individual account JSON may be corrupt or unreadable; skipping it keeps the rest of the list intact)
        try:
            with meta_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                continue
            # Ensure the name field is always set
            data.setdefault("name", meta_file.stem)
            accounts.append(data)
        except Exception:  # stx-allow: fallback (reason: skip corrupt/missing account metadata JSON — list call must tolerate partial store damage)
            continue
    return accounts


def save_account(
    name: str,
    metadata: dict[str, Any],
    store_dir: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Persist account metadata to the store.

    Args:
        name: Short identifier for the account (used as filename stem).
        metadata: Safe metadata dict (no tokens).
        store_dir: Override for the store directory.
        home: Override for the home directory.

    Returns:
        Path to the written metadata file.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    store.mkdir(parents=True, exist_ok=True)
    meta_file = store / f"{name}.json"
    payload = dict(metadata)
    payload["name"] = name
    tmp = meta_file.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    tmp.rename(meta_file)
    return meta_file


def delete_account(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> bool:
    """Remove an account from the store.

    Returns True if deleted, False if not found.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    meta_file = store / f"{name}.json"
    if not meta_file.exists():
        return False
    meta_file.unlink()
    # Remove credential snapshot directory if present
    cred_dir = store / name
    if cred_dir.is_dir():
        shutil.rmtree(cred_dir, ignore_errors=True)
    return True


def switch_account(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Switch the active Claude Code account to the named stored account.

    Copies the credential files from the account's snapshot directory into
    ``~/.claude/``.  The snapshot must have been created by ``sac account
    save <name>``.

    Args:
        name: Account name as used in ``save_account``.
        store_dir: Override for the store directory.
        home: Override for the home directory.

    Returns:
        Dict with ``success`` (bool), ``name`` (str), ``message`` (str).

    Never raises.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    cred_dir = store / name

    if not cred_dir.is_dir():
        return {
            "success": False,
            "name": name,
            "message": f"No credential snapshot found for account '{name}' at {cred_dir}",
        }

    claude_dir = _home / ".claude"
    # stx-allow: fallback (reason: ~/.claude/ may be on a read-only filesystem or a tmp copy may fail mid-flight; returning a failure dict is preferable to an unhandled exception)
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        for src in cred_dir.iterdir():
            dst = claude_dir / src.name
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copy2(src, tmp)
            tmp.rename(dst)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return {
            "success": False,
            "name": name,
            "message": f"Failed to copy credential files: {exc}",
        }

    return {
        "success": True,
        "name": name,
        "message": f"Switched to account '{name}'",
    }
