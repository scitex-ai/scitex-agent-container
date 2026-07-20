"""Collect a Codex subscription login into SAC's provider account store."""

from __future__ import annotations

import click


@click.command("sync-openai")
@click.option(
    "--name",
    default=None,
    help="Account slug when the Codex login has no display email.",
)
def sync_openai(name: str | None) -> None:
    """Collect the active Codex login for gateway rotation."""
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
