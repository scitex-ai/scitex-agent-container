"""Explicit identity-preserving recovery of one missing agent-owned checkout."""

from __future__ import annotations

import json

import click

from .._lifecycle._worktree_policy import WorktreePolicyError
from .._lifecycle._worktree_restore import restore_owned_task_worktree
from ..config import load_config
from ..config._resolve import resolve_with_prefix


def _require_owning_host(config) -> None:
    bound = config.hosts_spec.host
    if not bound:
        return
    from .._state.host_config import load as load_host_config
    from ..config._host import resolve_hostname
    from .lifecycle._common import _local_host_names
    from .lifecycle._host_chain import resolve_host_chain

    current = resolve_hostname()
    route = resolve_host_chain(
        bound, current, load_host_config().peers, local_names=_local_host_names(current)
    )
    if route.kind != "local":
        raise click.ClickException(
            f"restore must run on the declared owning host for {config.name}; "
            "invoke sac --on <owning-peer> agents restore-worktree there"
        )


@click.command(name="restore-worktree")
@click.argument("name")
@click.option(
    "--expected-tip", required=True, help="Exact retained full branch commit ID."
)
@click.option(
    "--apply", is_flag=True, help="Apply the exact previously reviewed receipt."
)
@click.option("--receipt-sha256", help="SHA256 emitted by the unchanged dry-run plan.")
def restore_worktree(
    name: str, expected_tip: str, apply: bool, receipt_sha256: str | None
) -> None:
    """Restore only NAME's absent recorded checkout, dry-run by default.

    Preserve its primary checkout, ownership record, existing branch and
    history. This operation never starts or replaces an agent. General Git
    command policy remains unchanged. Output is a reviewable JSON receipt.
    """
    if apply and not receipt_sha256:
        raise click.ClickException(
            "--apply requires the exact dry-run --receipt-sha256"
        )
    try:
        config = load_config(resolve_with_prefix(name))
        if config.name != name:
            raise click.ClickException(
                "restore requires the exact canonical agent name"
            )
        _require_owning_host(config)
        receipt = restore_owned_task_worktree(
            config,
            expected_tip=expected_tip,
            apply=apply,
            receipt_sha256=receipt_sha256,
        )
    except (WorktreePolicyError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(receipt, indent=2, sort_keys=True))


def register(agent_group: click.Group) -> None:
    agent_group.add_command(restore_worktree)


__all__ = ["register", "restore_worktree"]
