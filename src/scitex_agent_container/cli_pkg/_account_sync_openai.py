"""Collect a Codex subscription login into SAC's provider account store."""

from __future__ import annotations

import json

import click


@click.command("sync-openai")
@click.option(
    "--name",
    default=None,
    help="Account slug when the Codex login has no display email.",
)
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments without reading credentials."
)
@click.option(
    "--yes", "-y", is_flag=True, help="Approve collecting the local credential."
)
def sync_openai(name: str | None, dry_run: bool, yes: bool) -> None:
    """Collect the active Codex login for gateway rotation after approval.

    Preview reports arguments only. It does not read credentials, derive an
    account slug or validate a login. Approval copies local login material;
    this command does not authenticate with a provider or prove model access.
    Preview takes precedence over approval.

    \b
    Examples:
      $ sac accounts sync-openai --dry-run
      $ sac accounts sync-openai --yes
      $ sac accounts sync-openai --name shared --yes
    """
    if dry_run:
        click.echo(json.dumps({"action": "sync-openai", "dry_run": True, "name": name}))
        return
    if not yes:
        raise click.UsageError(
            "Collecting credentials requires --yes/-y; use --dry-run to preview."
        )
    from .._account.codex_account import CodexAccountSyncError, sync_codex_account

    try:
        destination = sync_codex_account(name=name)
    except CodexAccountSyncError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Collected OpenAI account in {destination.parent}")


def register_sync_openai_command(group: click.Group) -> None:
    """Attach the OpenAI collection command to the account group."""
    group.add_command(sync_openai)


__all__ = ["register_sync_openai_command", "sync_openai"]
