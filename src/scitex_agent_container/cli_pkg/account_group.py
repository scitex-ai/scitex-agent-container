"""CLI commands for account and quota management.

Provides the ``account`` subcommand group (save/list/delete/switch/status)
and the top-level ``quota-watch`` command.
"""

from __future__ import annotations

import click

# ---------------------------------------------------------------------------
# account group
# ---------------------------------------------------------------------------


@click.group("account")
def account() -> None:
    """Manage stored Claude Code accounts for credential rotation."""


# Credential auto-sync substrate (sync-live / watch-live) lives in its
# own module to keep this file under the per-file line cap; attach its
# commands onto the group at import time.
from ._account_sync_live import register_sync_live_commands

register_sync_live_commands(account)


@account.command("save")
@click.argument("name")
@click.option(
    "--email",
    default=None,
    help="Email address label for this account (informational only).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would be saved without writing any files.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (currently a no-op; reserved).",
)
def account_save(name: str, email: str | None, dry_run: bool, yes: bool) -> None:
    """Snapshot the current credentials under NAME for later rotation.

    \b
    Example:
      $ sac account save work
      $ sac account save work --email me@example.com
    """
    _ = yes  # accepted for API consistency; no prompt is currently shown.
    if dry_run:
        click.echo(
            f"[dry-run] would save account '{name}' (email={email or 'auto-detect'})"
        )
        return
    import shutil
    from pathlib import Path

    from .._state.account_store import _store_path, save_account

    home = Path.home()
    store = _store_path(None, home)
    cred_dir = store / name
    cred_dir.mkdir(parents=True, exist_ok=True)

    # Copy credential files into the snapshot directory
    claude_dir = home / ".claude"
    copied = []
    for fname in (".credentials.json",):
        src = claude_dir / fname
        if src.exists():
            shutil.copy2(src, cred_dir / fname)
            copied.append(fname)

    # Save metadata
    meta: dict = {}
    if email:
        meta["email_address"] = email
    else:
        # Try to read from current credentials
        # stx-allow: fallback (reason: reading existing email from credentials is best-effort; account save must still succeed without it)
        try:
            from .._account.credentials import read_credentials_metadata

            m = read_credentials_metadata(home=home)
            if m.get("email_address"):
                meta["email_address"] = m["email_address"]
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass

    save_account(name, meta, home=home)
    click.echo(
        f"Saved account '{name}' to {cred_dir} (files: {copied or 'none found'})"
    )


@account.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON array on stdout instead of human prose.",
)
@click.option(
    "--refresh",
    "--live",
    "refresh",
    is_flag=True,
    default=False,
    help=(
        "Force a fresh upstream usage fetch by discarding the per-account "
        "usage.json cache before rendering. Without this flag the 5-min "
        "cache is consulted to avoid hammering the API; the As-of column "
        "always shows the snapshot age so a stale number is obvious."
    ),
)
def account_list(as_json: bool, refresh: bool) -> None:
    """List stored accounts and show the currently active one.

    \b
    Example:
      $ sac account list
      $ sac account list --json
      $ sac account list --refresh    # force upstream usage% refetch
    """
    import json as _json

    from .._account.credentials import read_credentials_metadata
    from .._state.account_store import list_accounts
    from ._account_list_render import (
        build_stored_json,
        build_stored_rows,
        render_stored_table,
    )
    from ._helpers import console
    from .status_cmds import _format_claude_account_block

    accounts = list_accounts()

    if as_json:
        # stx-allow: fallback (reason: malformed credentials JSON tolerated)
        try:
            active = read_credentials_metadata()
        except (OSError, _json.JSONDecodeError):
            active = {}
        click.echo(
            _json.dumps(
                {
                    "active": active,
                    "stored": build_stored_json(accounts, refresh=refresh),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    # Active credentials block
    # stx-allow: fallback (reason: malformed credentials JSON tolerated; section omitted on error)
    try:
        active_meta = read_credentials_metadata()
    except (OSError, _json.JSONDecodeError):
        active_meta = {}
    lines = _format_claude_account_block(active_meta)
    for line in lines:
        console.print(line)
    if lines:
        console.print("")

    if not accounts:
        click.echo(
            "No accounts stored. Use: scitex-agent-container account save <name>"
        )
        return
    console.print(render_stored_table(build_stored_rows(accounts, refresh=refresh)))


@account.command("delete")
@click.argument("name")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be deleted without removing anything.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def account_delete(name: str, dry_run: bool, yes: bool) -> None:
    """Remove a stored account.

    \b
    Example:
      $ sac account delete work
      $ sac account delete work --dry-run
      $ sac account delete work --yes
    """
    from .._state.account_store import delete_account

    if dry_run:
        click.echo(f"[dry-run] would delete account '{name}'")
        return
    if not yes:
        click.echo(f"Refusing to delete account '{name}' without --yes/-y.", err=True)
        raise SystemExit(2)
    if delete_account(name):
        click.echo(f"Deleted account '{name}'")
    else:
        click.echo(f"Account '{name}' not found", err=True)
        raise SystemExit(1)


@account.command("switch")
@click.argument("name")
def account_switch(name: str) -> None:
    """Switch active credentials to a stored account.

    \b
    Example:
      $ sac account switch work
    """
    from .._state.account_store import switch_account

    result = switch_account(name)
    if result["success"]:
        click.echo(result["message"])
    else:
        click.echo(result["message"], err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# quota-watch — exposed both at top-level (legacy) and under ``account``.
# ---------------------------------------------------------------------------


@account.command("watch-quota")
@click.option(
    "--threshold",
    default=80.0,
    show_default=True,
    help="Rotate when usage exceeds this %.",
)
@click.option(
    "--interval",
    default=300,
    show_default=True,
    help="Check interval in seconds.",
)
@click.option("--dry-run", is_flag=True, help="Check but do not actually rotate.")
@click.option("--once", is_flag=True, help="Run once instead of looping.")
@click.option(
    "--daemon",
    is_flag=True,
    help="Double-fork into background (UNIX only). Logs to --log-file.",
)
@click.option(
    "--log-file",
    default=None,
    show_default=False,
    help="Log file path when running as daemon (default: ~/.scitex/logs/quota-watch.log).",
)
def account_watch_quota(
    threshold: float,
    interval: int,
    dry_run: bool,
    once: bool,
    daemon: bool,
    log_file: str | None,
) -> None:
    """Monitor quota and auto-rotate credentials when threshold exceeded.

    \b
    Examples:
      $ sac account watch-quota --once
      $ sac account watch-quota
      $ sac account watch-quota --daemon
    """
    from pathlib import Path

    from .._account.quota_watch import check_and_rotate, run_loop, survival_mode_check

    if once or dry_run:
        result = check_and_rotate(threshold=threshold, dry_run=dry_run)
        click.echo(f"[{result['action']}] {result['message']}")
        sv = survival_mode_check()
        if sv["survival_mode"]:
            click.echo(f"[SURVIVAL] {sv['message']}", err=True)
        return

    log_path = Path(log_file) if log_file else None
    if daemon:
        click.echo(
            f"Forking quota-watch daemon (interval={interval}s, threshold={threshold}%). "
            f"Log: {log_path or '~/.scitex/logs/quota-watch.log'}"
        )
    run_loop(
        threshold=threshold,
        interval=interval,
        daemon=daemon,
        log_path=log_path,
    )


# ---------------------------------------------------------------------------
# status — one-shot quota snapshot, optionally over ssh to a peer.
# ---------------------------------------------------------------------------


@account.command("status")
@click.option(
    "--host",
    "host",
    default=None,
    help=(
        "Peer name from ~/.scitex/agent-container/config.yaml. When set, "
        "ssh to that peer and run `sac accounts status --json` there."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON object instead of human prose.",
)
def account_status(host: str | None, as_json: bool) -> None:
    """One-shot quota snapshot (5h%, 7d%, account email + tier).

    \b
    Examples:
      $ sac accounts status
      $ sac accounts status --json
      $ sac accounts status --host spartan
    """
    import json as _json

    from ._account_status import (
        StatusError,
        collect_status,
        collect_status_remote,
        format_status_prose,
    )

    try:
        if host is not None:
            snapshot = collect_status_remote(host)
        else:
            snapshot = collect_status()
    except StatusError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(_json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        click.echo(format_status_prose(snapshot))


@click.command("watch-quota")
@click.option(
    "--threshold",
    default=80.0,
    show_default=True,
    help="Rotate when usage exceeds this %.",
)
@click.option(
    "--interval",
    default=300,
    show_default=True,
    help="Check interval in seconds.",
)
@click.option("--dry-run", is_flag=True, help="Check but do not actually rotate.")
@click.option("--once", is_flag=True, help="Run once instead of looping.")
@click.option(
    "--daemon",
    is_flag=True,
    help="Double-fork into background (UNIX only). Logs to --log-file.",
)
@click.option(
    "--log-file",
    default=None,
    show_default=False,
    help="Log file path when running as daemon (default: ~/.scitex/logs/quota-watch.log).",
)
def quota_watch(
    threshold: float,
    interval: int,
    dry_run: bool,
    once: bool,
    daemon: bool,
    log_file: str | None,
) -> None:
    """Monitor quota and auto-rotate credentials when threshold exceeded.

    \b
    Examples:
      # single check
      scitex-agent-container watch-quota --once
      # foreground loop every 5 min
      scitex-agent-container watch-quota
      # background daemon
      scitex-agent-container watch-quota --daemon
    """
    from pathlib import Path

    from .._account.quota_watch import check_and_rotate, run_loop, survival_mode_check

    if once or dry_run:
        result = check_and_rotate(threshold=threshold, dry_run=dry_run)
        click.echo(f"[{result['action']}] {result['message']}")
        # Also report survival mode in single-check mode
        sv = survival_mode_check()
        if sv["survival_mode"]:
            click.echo(f"[SURVIVAL] {sv['message']}", err=True)
        return

    log_path = Path(log_file) if log_file else None
    if daemon:
        click.echo(
            f"Forking quota-watch daemon (interval={interval}s, threshold={threshold}%). "
            f"Log: {log_path or '~/.scitex/logs/quota-watch.log'}"
        )
    run_loop(
        threshold=threshold,
        interval=interval,
        daemon=daemon,
        log_path=log_path,
    )


# ---------------------------------------------------------------------------
# refresh — headless OAuth access-token rotation (no `claude /login` prompt).
# Lives in its own module to keep this file under the per-file line cap;
# attached onto the group at import time (same pattern as sync-live).
# ---------------------------------------------------------------------------
from ._account_refresh import register_refresh_command

register_refresh_command(account)
