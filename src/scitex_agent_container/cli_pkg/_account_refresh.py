"""``sac accounts refresh`` — headless OAuth access-token rotation.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_refresh_command` (same pattern as
``_account_sync_live.register_sync_live_commands``).

As long as the (long-lived) refresh_token is still valid, the
access_token is rotated in place and atomically written back to the same
per-account credentials file — eliminating routine manual ``claude
/login`` for stored accounts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click


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


@click.command("refresh")
@click.argument("name", required=False)
@click.option(
    "--all",
    "do_all",
    is_flag=True,
    default=False,
    help="Refresh every stored account in turn (one network call each).",
)
@click.option(
    "--skip-active",
    "skip_active",
    is_flag=True,
    default=False,
    help=(
        "With --all, exclude the account matching the currently-active "
        "~/.claude login (avoids rotating the in-use refresh_token and "
        "breaking the live session)."
    ),
)
@click.option(
    "--include-active",
    "include_active",
    is_flag=True,
    default=False,
    help=(
        "With --all, refresh EVERY stored account INCLUDING the active "
        "and any pinned-running one (skip nothing). This is the mode the "
        "host-side sac-accounts-refresh timer runs under the master-host "
        "single-refresher model: agents bind the credential :ro and never "
        "refresh, so the timer must refresh the active account too or its "
        "agents die at token expiry. Mutually exclusive with --skip-active."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON array on stdout instead of human prose.",
)
def account_refresh(
    name: str | None,
    do_all: bool,
    skip_active: bool,
    include_active: bool,
    as_json: bool,
) -> None:
    """Mint a fresh access_token from the stored refresh_token, headlessly.

    Eliminates routine manual `claude /login`: as long as the (long-lived)
    refresh_token is still valid, the access_token is rotated in place,
    atomically written back to the same per-account credentials file.

    A real `claude /login` is only required when the refresh_token itself
    has lapsed — in which case this command reports it per-account and
    moves on (with ``--all``) rather than aborting the whole run.

    ``--skip-active`` (with ``--all``) excludes the account that matches
    the currently-active ``~/.claude`` login, so the refresh job never
    rotates the in-use refresh_token out from under the live session.

    ``--include-active`` (with ``--all``) is the opposite intent, made
    explicit: refresh EVERY account including the active + pinned-running
    ones (skip nothing). Under the master-host single-refresher model
    (2026-07-08) agents bind the credential ``:ro`` and never refresh, so
    the host-side ``sac-accounts-refresh`` timer is the SOLE refresher and
    MUST refresh the active account too — otherwise the active account's
    agents die when its access_token expires. The timer's ExecStart uses
    this flag. Mutually exclusive with ``--skip-active``.

    \b
    Examples:
      $ sac accounts refresh work
      $ sac accounts refresh --all
      $ sac accounts refresh --all --skip-active
      $ sac accounts refresh --all --include-active   # timer mode
      $ sac accounts refresh --all --json
    """
    import json as _json

    from .._account.claude_usage import refresh_account_credentials
    from .._state.account_store import _store_path, list_accounts

    if not do_all and not name:
        click.echo(
            "error: provide an account name or --all "
            "(see `sac accounts refresh --help`)",
            err=True,
        )
        raise SystemExit(2)
    if do_all and name:
        click.echo("error: pass either a name or --all, not both", err=True)
        raise SystemExit(2)
    if skip_active and include_active:
        click.echo(
            "error: --skip-active and --include-active are mutually "
            "exclusive (one skips the active account, the other forces it in)",
            err=True,
        )
        raise SystemExit(2)

    home = Path.home()
    store = _store_path(None, home)

    if do_all:
        accounts = list_accounts(home=home)
        targets = [a["name"] for a in accounts]
        if include_active:
            # Master-host single-refresher: refresh everything, skip
            # nothing. Log it so the operator can see the timer's intent.
            click.echo(
                "[include-active] refreshing ALL accounts including the "
                "active + pinned-running ones (single-refresher model).",
                err=True,
            )
        if skip_active:
            active_name = _resolve_active_account_name(home, accounts)
            pinned_running = _collect_pinned_running_accounts(home)
            if active_name is None:
                click.echo(
                    "[skip-active] no active account resolvable; "
                    "host-active skip is a no-op.",
                    err=True,
                )
            elif active_name in targets:
                click.echo(
                    f"[skip-active] excluding active account '{active_name}'.",
                    err=True,
                )
            for pinned_name in sorted(pinned_running & set(targets)):
                click.echo(
                    f"[skip-active] excluding pinned-running account "
                    f"'{pinned_name}' (refresh-token rotation race guard).",
                    err=True,
                )
            skip_set: set[str] = set(pinned_running)
            if active_name:
                skip_set.add(active_name)
            targets = [t for t in targets if t not in skip_set]
    else:
        targets = [name]  # type: ignore[list-item]

    results: list[dict] = []
    for acct_name in targets:
        creds_path = store / acct_name / ".credentials.json"
        # stx-allow: fallback (reason: refresh_account_credentials is documented never-raise, but defence-in-depth so one bad row never crashes --all)
        try:
            r = refresh_account_credentials(creds_path)
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            r = {
                "success": False,
                "expires_at": None,
                "error": f"unexpected error: {exc}",
                "credentials_path": str(creds_path),
            }
        r["name"] = acct_name
        results.append(r)

    if as_json:
        click.echo(_json.dumps(results, ensure_ascii=False, indent=2))
    else:
        if not results:
            click.echo("No accounts stored. Use: sac accounts save <name>")
        for r in results:
            if r["success"]:
                click.echo(
                    f"  {r['name']:20s}  refreshed; new expiry "
                    f"{r['expires_at'] or '(unknown)'}"
                )
            else:
                click.echo(f"  {r['name']:20s}  FAILED — {r['error']}", err=True)

    # Exit non-zero only if EVERY target failed; --all with mixed results
    # is still a useful partial success.
    if results and not any(r["success"] for r in results):
        raise SystemExit(1)


def register_refresh_command(group: click.Group) -> None:
    """Attach the ``refresh`` command onto the ``account`` group."""
    group.add_command(account_refresh)


__all__ = ["account_refresh", "register_refresh_command"]
