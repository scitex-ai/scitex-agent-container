"""Credential account store for multi-account rotation.

Manages a directory of saved Claude Code accounts.  Each account is a
self-contained directory under ``~/.scitex/agent-container/accounts/<name>/``
holding:

  * ``account.json``     — safe metadata only (no tokens).
  * ``.credentials.json`` (and any other Claude credential files) — copied
    into ``~/.claude/`` by ``switch_account``.

Design rules
------------
1. No token material is ever stored in the account metadata JSON.
2. ``list_accounts()`` never raises.
3. ``switch_account()`` copies credential files atomically and never
   propagates the metadata file into ``~/.claude/``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

_DEFAULT_STORE_SUBDIR = Path(".scitex") / "agent-container" / "accounts"
_METADATA_FILENAME = "account.json"

# ``sac`` is a first-class short alias for ``agent-container``.
# Both names live under ``~/.scitex/``; the short one is a symlink to
# the canonical long one so muscle-memory tab-completion works.
# Self-healed on every store touch so it's never missed on a new host.
_SHORT_ROOT_NAME = "sac"
_CANONICAL_ROOT_NAME = "agent-container"


def _ensure_short_name_alias(home: Path) -> None:
    """Idempotently maintain ``~/.scitex/sac -> agent-container`` and
    the agent-container root .gitignore (see ``_bootstrap``).

    Skips the symlink if the short-name path already exists as a real
    directory (refuses to clobber user data — the migration script
    handles that case explicitly).
    """
    scitex_root = home / ".scitex"
    short = scitex_root / _SHORT_ROOT_NAME
    # Seed the root .gitignore on every touch — idempotent + best-effort.
    from ._bootstrap import ensure_root_gitignore

    ensure_root_gitignore(scitex_root / _CANONICAL_ROOT_NAME)
    if short.is_symlink():
        return
    if short.exists():
        return  # real dir — refuse to clobber; migration script handles it
    # stx-allow: fallback (reason: best-effort symlink convenience; failure must not break the actual account write the caller is doing)
    try:
        scitex_root.mkdir(parents=True, exist_ok=True)
        short.symlink_to(_CANONICAL_ROOT_NAME, target_is_directory=True)
    except OSError:
        pass


def _store_path(store_dir: Path | None, home: Path) -> Path:
    if store_dir is not None:
        return Path(store_dir)
    _ensure_short_name_alias(home)
    # Test fixtures pass an explicit `home=tmp_path`; honour it
    # literally rather than walking the cascade (which keys off
    # `Path.cwd()` and would resolve outside the test's tmp dir).
    if home != Path.home():
        return home / _DEFAULT_STORE_SUBDIR
    # SciTeX local-state cascade: project-scope
    # `<repo>/.scitex/agent-container/accounts/` wins, falls back to
    # `$SCITEX_DIR/agent-container/accounts/` (default `~/.scitex/...`).
    # See `01_ecosystem_06_local-state-directories.md` §4a (tracked
    # state — credentials travel with the project when versioned).
    from scitex_config._ecosystem import local_state as _local_state

    return _local_state.path("agent-container", "accounts")


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
    for account_dir in sorted(p for p in store.iterdir() if p.is_dir()):
        # Skip non-account subdirs (e.g. `_rotations/` holding
        # auth-rotation telemetry NDJSON files keyed by email).
        if account_dir.name.startswith("_"):
            continue
        meta_file = account_dir / _METADATA_FILENAME
        # stx-allow: fallback (reason: individual account dir may be corrupt or unreadable; skipping it keeps the rest of the list intact)
        try:
            if meta_file.is_file():
                with meta_file.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    continue
            else:
                data = {}
            data.setdefault("name", account_dir.name)
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
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    meta_file = account_dir / _METADATA_FILENAME
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
    account_dir = store / name
    if not account_dir.is_dir():
        return False
    shutil.rmtree(account_dir, ignore_errors=True)
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
    account_dir = store / name

    if not account_dir.is_dir():
        return {
            "success": False,
            "name": name,
            "message": f"No account directory for '{name}' at {account_dir}",
        }

    claude_dir = _home / ".claude"
    # stx-allow: fallback (reason: ~/.claude/ may be on a read-only filesystem or a tmp copy may fail mid-flight; returning a failure dict is preferable to an unhandled exception)
    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        for src in account_dir.iterdir():
            if src.name == _METADATA_FILENAME:
                continue  # metadata stays in the account dir; never copy into ~/.claude/
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
