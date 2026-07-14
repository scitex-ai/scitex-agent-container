"""Skip-set helpers for ``sac accounts refresh --all --skip-active``.

Extracted from ``_account_refresh.py`` to keep that command module under
the per-file line cap. These three helpers build the set of stored
accounts that ``--skip-active`` must EXCLUDE from an ``--all`` refresh:

* :func:`_resolve_active_account_name` — the account matching the live
  ``~/.claude`` login (by email);
* :func:`_resolve_registry_dir` / :func:`_collect_pinned_running_accounts`
  — accounts currently pinned by a running local agent (refresh-token
  rotation-race guard).

None of this is used by the daemon's ``--sync-active-login`` mode (which
refreshes the active account on purpose); it remains for the explicit
``--skip-active`` opt-in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _resolve_active_account_name(
    home: Path, accounts: list[dict[str, Any]]
) -> str | None:
    """Return the stored-account NAME whose identity matches the live login.

    The active account is the one currently logged in under ``~/.claude/``
    — identified by the email surfaced in ``~/.claude.json``
    (``oauthAccount.emailAddress``), the same field ``sac accounts list``
    and ``sac accounts sync-live`` key off. We compare that email against
    each stored account's ``email_address`` (saved into ``account.json`` by
    ``sac accounts save`` / auto-sync), case-insensitively.

    Returns the matching stored name, or ``None`` when no active email can
    be resolved (no live login, malformed file) or when no stored account
    carries that email — in which case the caller skips nothing and logs
    it. Never raises.
    """
    # stx-allow: fallback (reason: active-account resolution is a
    # best-effort guard for --skip-active; any read/parse failure maps to
    # "cannot resolve" so refresh proceeds without skipping, never crashes.)
    try:
        from .._account.credentials import read_credentials_metadata

        active = read_credentials_metadata(home=home)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    active_email = active.get("email_address")
    if not isinstance(active_email, str) or not active_email.strip():
        return None
    active_email_norm = active_email.strip().lower()
    for acct in accounts:
        stored_email = acct.get("email_address")
        if (
            isinstance(stored_email, str)
            and stored_email.strip().lower() == active_email_norm
        ):
            name = acct.get("name")
            return name if isinstance(name, str) else None
    return None


def _resolve_registry_dir(home: Path) -> Path:
    """Resolve the file-based agent registry directory.

    Mirrors the ``Registry`` class's path-resolution rule (honors
    ``SCITEX_AGENT_CONTAINER_REGISTRY_DIR``, else
    ``<home>/.scitex/agent-container/runtime/registry``), but READ per
    call rather than frozen at module-import time. The freeze on
    ``Registry`` is fine in production (HOME doesn't change) but is
    brittle under pytest where the sandbox HOME is set after the test
    process started.
    """
    import os

    env_override = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    if env_override:
        return Path(env_override)
    return home / ".scitex" / "agent-container" / "runtime" / "registry"


def _collect_pinned_running_accounts(home: Path | None = None) -> set[str]:
    """Return stored-account NAMES currently pinned by running local agents.

    Closes the refresh-token rotation race: when an agent is spawned with
    ``spec.claude.account: <name>``, its in-container Claude CLI refreshes
    that account's token through the live :rw dir-bind on the snapshot.
    If the host ``sac accounts refresh --all --skip-active`` cron ALSO
    refreshes the same account every 2h, the two refreshers rotate each
    other's refresh_token (OAuth refresh-tokens invalidate on use) and
    whichever raced last leaves the other with a now-invalid token —
    next API call hits 401.

    This helper enumerates the local file-based agent registry (the same
    JSON files ``sac status`` reads, written by ``_lifecycle/_start`` at
    spawn time), loads each entry's ``config`` spec, and extracts
    ``spec.claude.account`` when set. The caller unions the result with
    the host-active account into a single skip-set so the cron never
    refreshes a token currently in use by a running agent.

    Cross-host scope: only LOCAL running agents matter — the cron runs
    against the LOCAL snapshot store, and agents on other hosts have
    their own local snapshot stores (and their own refresher cron).

    Stale-registry tolerance: a registry entry left behind by a crashed
    agent will over-skip its account (the cron won't refresh it until
    the stale entry is cleaned via ``Registry.cleanup_stale``). The
    failure mode is safe: under-refresh, eventually requiring manual
    ``sac accounts refresh <name>``. The opposite (over-refresh racing
    a live agent) is the bug we're fixing.

    Tolerant: any registry / spec read failure is mapped to "this entry
    contributes nothing" so refresh proceeds against the rest of the
    set rather than crashing on one bad row.
    """
    import json as _json

    home = home if home is not None else Path.home()
    reg_dir = _resolve_registry_dir(home)
    pinned: set[str] = set()
    if not reg_dir.is_dir():
        return pinned
    # stx-allow: fallback (reason: skip-set construction is best-effort;
    # any registry / spec read failure maps to "skip nothing extra" so
    # refresh proceeds against the remaining accounts.)
    try:
        from ..config import load_config
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return pinned
    for entry_path in sorted(reg_dir.glob("*.json")):
        try:
            entry = _json.loads(entry_path.read_text())
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            continue
        cfg_path = entry.get("config") if isinstance(entry, dict) else None
        if not isinstance(cfg_path, str) or not cfg_path:
            continue
        try:
            cfg = load_config(Path(cfg_path))
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            continue
        acct = getattr(getattr(cfg, "claude", None), "account", "") or ""
        if isinstance(acct, str) and acct.strip():
            pinned.add(acct.strip())
    return pinned


__all__ = [
    "_collect_pinned_running_accounts",
    "_resolve_active_account_name",
    "_resolve_registry_dir",
]
