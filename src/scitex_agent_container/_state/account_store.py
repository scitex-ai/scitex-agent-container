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
        if account_dir.name.startswith("_") or account_dir.name == "openai":
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


def read_account_plan(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Read the OFFLINE plan/tier of a saved account from its snapshot.

    Opens ``<acct>/.credentials.json`` and pulls ONLY the two non-secret
    fields ``subscriptionType`` and ``rateLimitTier`` (whitelist — tokens
    are never touched), then derives a human ``plan_label`` (Pro / Max 5x
    / Max 20x / Free) via the same mapping ``read_credentials_metadata``
    uses. No network call — this is free and works for ALL stored
    accounts, not just the active one.

    Returns a dict with keys ``subscription_type``, ``rate_limit_tier``,
    ``plan_label`` (any may be ``None``). Never raises — a
    missing/corrupt snapshot yields all-``None``.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    snapshot = store / name / ".credentials.json"
    out: dict[str, Any] = {
        "subscription_type": None,
        "rate_limit_tier": None,
        "plan_label": None,
    }
    # stx-allow: fallback (reason: snapshot read is best-effort offline
    # enrichment for `account list`; a missing/corrupt snapshot must
    # degrade to all-None, never break the listing.)
    try:
        with snapshot.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return out
    if not isinstance(data, dict):
        return out
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return out
    sub = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    if isinstance(sub, str):
        out["subscription_type"] = sub
    if isinstance(tier, str):
        out["rate_limit_tier"] = tier
    # Derive label via the canonical credentials mapping.
    # stx-allow: fallback (reason: label derivation is cosmetic; raw
    # tier/subscription stay exposed even if the import/derive hiccups.)
    try:
        from .._account.credentials import _derive_plan_label

        out["plan_label"] = _derive_plan_label(
            out["rate_limit_tier"], out["subscription_type"]
        )
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        out["plan_label"] = None
    return out


def read_account_usage_cache(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any] | None:
    """Read a CACHED per-account usage snapshot, if one exists.

    5h/7d usage (``used_pct_5h`` / ``used_pct_7d``) is NOT in the
    credential snapshot and needs a per-account network call with a
    credential swap to fetch — too expensive to do synchronously inside
    ``account list``. This reader is CACHE-ONLY: it returns the contents
    of ``<acct>/usage.json`` (with an ``as_of`` timestamp) when present,
    else ``None`` so the caller can render ``"—"``.

    NOTE: nothing writes ``<acct>/usage.json`` yet. ``sac account
    watch-quota`` is the natural place to persist a per-account usage
    snapshot per rotation — wiring that writer is intentionally out of
    scope for the per-agent-account feature (it needs the credential-swap
    fetch loop). This reader exists so the display path is ready the day
    that writer lands. Never raises.
    """
    _home = home or Path.home()
    store = _store_path(store_dir, _home)
    usage_file = store / name / "usage.json"
    # stx-allow: fallback (reason: cache-only read; missing/corrupt cache
    # must yield None so `account list` shows "—", never break the list.)
    try:
        with usage_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    if not isinstance(data, dict):
        return None
    return data


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


def _read_access_token_fingerprint(creds_path: Path) -> str | None:
    """Best-effort OPAQUE fingerprint of a ``.credentials.json`` access token.

    Reads ``claudeAiOauth.accessToken`` and returns its one-way
    ``sha256:<hex>`` fingerprint (never the token itself). ``None`` on any
    missing/corrupt file. Used only to make a token FROM→TO rotation
    visible in the audit record. Never raises.
    """
    # stx-allow: fallback (reason: fingerprint is a cosmetic audit field;
    # a missing/corrupt live-or-store credential must degrade to None, never
    # break the switch the caller is performing.)
    try:
        from .._account._rotation_audit import fingerprint_token

        data = json.loads(creds_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        return fingerprint_token(oauth.get("accessToken"))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


def switch_account(
    name: str,
    store_dir: Path | None = None,
    home: Path | None = None,
    *,
    event: str = "switch",
    reason: str = "manual switch",
    from_account: str | None = None,
) -> dict[str, Any]:
    """Switch the active Claude Code account to the named stored account.

    Copies the credential files from the account's snapshot directory into
    ``~/.claude/``.  The snapshot must have been created by ``sac account
    save <name>``.

    On success a structured rotation-audit record is appended (see
    :mod:`.._account._rotation_audit`) capturing WHAT rotated FROM→TO and
    WHY, with opaque token fingerprints (never the tokens themselves).

    Args:
        name: Account name as used in ``save_account``.
        store_dir: Override for the store directory.
        home: Override for the home directory.
        event: Audit event kind — ``"switch"`` (manual) or
            ``"auto-rotate"`` (quota-watch). Callers that rotate for a
            different reason pass their own event/reason.
        reason: Human/trigger string recorded as the audit ``reason``.
        from_account: The account rotating away from (recorded in the
            audit). ``None`` → best-effort resolution from the live login.

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
    live_creds = claude_dir / ".credentials.json"
    # Capture the OUTGOING (live) token fingerprint BEFORE we overwrite it.
    from_token_fp = _read_access_token_fingerprint(live_creds)
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

    # --- Rotation audit (best-effort, never breaks the switch) -------------
    # stx-allow: fallback (reason: the audit record is a durable side-effect;
    # a failure to write it must never fail the credential switch itself.)
    try:
        from .._account._rotation_audit import log_rotation_event

        resolved_from = from_account
        if resolved_from is None:
            # Best-effort: label the outgoing account from the live login.
            resolved_from = _read_active_account_email(_home)
        to_token_fp = _read_access_token_fingerprint(account_dir / ".credentials.json")
        log_rotation_event(
            store=store,
            event=event,
            from_account=resolved_from,
            to_account=name,
            reason=reason,
            from_token_fp=from_token_fp,
            to_token_fp=to_token_fp,
        )
    except Exception:  # stx-allow: fallback (reason: audit is best-effort; never fail the switch on it)
        pass

    return {
        "success": True,
        "name": name,
        "message": f"Switched to account '{name}'",
    }


def _read_active_account_email(home: Path) -> str | None:
    """Return the active-login email from ``~/.claude.json``, best-effort.

    Reads ``oauthAccount.emailAddress`` — the same field the rest of sac
    keys the active account off. ``None`` on any missing/corrupt file.
    Used only to label the ``from_account`` on a manual switch. Never
    raises.
    """
    # stx-allow: fallback (reason: active-email is a cosmetic audit label;
    # a missing/mid-rewrite ~/.claude.json degrades to None, never crashes.)
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        oauth = data.get("oauthAccount")
        if not isinstance(oauth, dict):
            return None
        email = oauth.get("emailAddress")
        if isinstance(email, str) and email.strip():
            return email.strip()
        return None
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
