"""CLI commands for account and quota management.

Provides the ``account`` subcommand group (save/list/delete/switch) and
the top-level ``quota-watch`` command.
"""

from __future__ import annotations

import click


# ---------------------------------------------------------------------------
# account group
# ---------------------------------------------------------------------------


@click.group("account")
def account() -> None:
    """Manage stored Claude Code accounts for credential rotation."""


@account.command("save")
@click.argument("name")
@click.option(
    "--email",
    default=None,
    help="Email address label for this account (informational only).",
)
def account_save(name: str, email: str | None) -> None:
    """Snapshot the current credentials under NAME for later rotation."""
    import shutil
    from pathlib import Path
    from ..account_store import save_account, _store_path

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
        try:
            from ..credentials import read_credentials_metadata

            m = read_credentials_metadata(home=home)
            if m.get("email_address"):
                meta["email_address"] = m["email_address"]
        except Exception:
            pass

    save_account(name, meta, home=home)
    click.echo(f"Saved account '{name}' (files: {copied or 'none found'})")


@account.command("list")
def account_list() -> None:
    """List all stored accounts."""
    from ..account_store import list_accounts

    accounts = list_accounts()
    if not accounts:
        click.echo("No accounts stored. Use: scitex-agent-container account save <name>")
        return
    for acct in accounts:
        email = acct.get("email_address") or "(no email)"
        click.echo(f"  {acct['name']:20s}  {email}")


@account.command("delete")
@click.argument("name")
def account_delete(name: str) -> None:
    """Remove a stored account."""
    from ..account_store import delete_account

    if delete_account(name):
        click.echo(f"Deleted account '{name}'")
    else:
        click.echo(f"Account '{name}' not found", err=True)
        raise SystemExit(1)


@account.command("switch")
@click.argument("name")
def account_switch(name: str) -> None:
    """Switch active credentials to a stored account."""
    from ..account_store import switch_account

    result = switch_account(name)
    if result["success"]:
        click.echo(result["message"])
    else:
        click.echo(result["message"], err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# quota-watch top-level command
# ---------------------------------------------------------------------------


@click.command("quota-watch")
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
def quota_watch(threshold: float, interval: int, dry_run: bool, once: bool) -> None:
    """Monitor quota and auto-rotate credentials when threshold exceeded."""
    from ..quota_watch import check_and_rotate, run_loop

    if once or dry_run:
        result = check_and_rotate(threshold=threshold, dry_run=dry_run)
        click.echo(f"[{result['action']}] {result['message']}")
        return
    run_loop(threshold=threshold, interval=interval)
