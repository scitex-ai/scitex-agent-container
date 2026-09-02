"""``sac a2a {grant,unblock,block,revoke,grants}`` — the cross-group ACL verbs.

Extracted from :mod:`.a2a_group` (over the per-file cap) and registered
onto it by :func:`register`, the same way :mod:`._host_sync` attaches to
``sac host``. Thin click wrappers over the cross-group ACL primitives in
``_state.state_db_nodes`` (``grant_send`` / ``revoke_send`` /
``list_comms_grants``). Operators previously had to drop into a Python
REPL to amend the comms-grants table — a footgun (silently granting too
much on wrong argument order). The CLI makes it auditable and validates
the positional order at the Click layer.

Imports happen inside the callbacks (not at module import) to keep the
Click cold-start cheap: ``sac --help`` and tab-completion press should
never load a database driver. The same lazy pattern used by
``host_group`` / ``peer_group`` for state_db consumers.
"""

from __future__ import annotations

import json

import click

__all__ = [
    "a2a_block",
    "a2a_grant",
    "a2a_grants",
    "a2a_revoke",
    "a2a_unblock",
    "register",
]


def _do_unblock(sender: str, target: str, note: str | None) -> None:
    """Shared implementation for both ``grant`` (legacy) and ``unblock``.

    Task #27 PR B: dispatches via
    :func:`_a2a_acl_dispatch.dispatch_acl_decision` which routes
    in-SIF → host listen HTTP (so the write lands on the host's
    state.db) and bare-host → local DB helpers directly.
    """
    from ._a2a_acl_dispatch import dispatch_acl_decision

    try:
        result = dispatch_acl_decision(
            "unblock", sender=sender, target=target, note=note
        )
    except click.ClickException:
        raise
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        # In-SIF broker raised an AclBrokerError (or transport
        # error). Surface as a clean ClickException so the
        # operator sees a single-line stderr instead of a
        # traceback.
        raise click.ClickException(str(exc)) from exc
    from ._helpers import console

    extras = []
    if result.get("unblocked"):
        extras.append("removed block")
    if result.get("cleared_pending"):
        extras.append("cleared pending prompt")
    tail = f" [dim]({'; '.join(extras)})[/dim]" if extras else ""
    console.print(f"[green]ok[/green]  unblocked  {sender}  ->  {target}{tail}")


@click.command("grant")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help="Free-form audit annotation (e.g. ticket / handoff that authorised this).",
)
def a2a_grant(sender: str, target: str, note: str | None) -> None:
    """Grant ``SENDER`` permission to send messages to ``TARGET``.

    Legacy alias of ``sac a2a unblock <SENDER> <TARGET>``. Writes the
    ``comms_grants`` row, removes any ``comms_blocks`` row, and
    clears the pending-prompt row for the pair. Re-granting an
    already-granted pair is a no-op on the timestamp.

    Argument order matters: ``SENDER → TARGET`` is directional. To
    allow bidirectional cross-group traffic, run the command twice.

    \b
    Example:
      $ sac a2a grant worker-a worker-b
      $ sac a2a grant worker-a worker-b --note "ticket-PA-512"
    """
    _do_unblock(sender, target, note)


@click.command("unblock")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help=(
        "Free-form audit annotation (e.g. the approval-prompt msg_id this "
        "responds to)."
    ),
)
def a2a_unblock(sender: str, target: str, note: str | None) -> None:
    """UNBLOCK ``SENDER`` — allow this sender's future messages to ``TARGET``.

    Task #27 receiver-facing verb. Embedded in the approve-prompt
    push the receiver sees on a denied cross-group send. Writes the
    ``comms_grants`` row, removes any ``comms_blocks`` row, and
    clears the pending-prompt row. The sender's original denied
    message is NOT replayed — they resend if needed.

    \b
    Example:
      $ sac a2a unblock worker-a lead
      $ sac a2a unblock worker-a lead --note "prompt msg_id abc123"
    """
    _do_unblock(sender, target, note)


@click.command("block")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help=(
        "Free-form audit annotation (e.g. the approval-prompt msg_id this "
        "responds to)."
    ),
)
def a2a_block(sender: str, target: str, note: str | None) -> None:
    """BLOCK ``SENDER`` — silently drop this sender's future attempts to ``TARGET``.

    Task #27 receiver-facing verb. Embedded in the approve-prompt
    push as the silence-this-sender alternative to ``unblock``.
    Writes the ``comms_blocks`` row, clears the pending-prompt row.
    Future sends from ``SENDER`` to ``TARGET`` are silently dropped
    by :func:`_listen._acl.check_send_acl` (no receiver push, no
    approve-prompt re-fire). The sender still gets a 403 — they
    learn their send did not land — but the receiver sees nothing.

    Idempotent: re-blocking is a no-op on the existing row's
    timestamp. Block precedence: if the pair also has a grant,
    BLOCK wins.

    \b
    Example:
      $ sac a2a block worker-a lead
      $ sac a2a block worker-a lead --note "prompt msg_id abc123"
    """
    from ._a2a_acl_dispatch import dispatch_acl_decision

    try:
        result = dispatch_acl_decision("block", sender=sender, target=target, note=note)
    except click.ClickException:
        raise
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    from ._helpers import console

    tail = (
        " [dim](cleared pending prompt)[/dim]" if result.get("cleared_pending") else ""
    )
    console.print(f"[yellow]ok[/yellow]  blocked  {sender}  ->  {target}{tail}")


@click.command("revoke")
@click.argument("sender")
@click.argument("target")
def a2a_revoke(sender: str, target: str) -> None:
    """Revoke ``SENDER``'s permission to send messages to ``TARGET``.

    Thin wrapper over ``_state.state_db_nodes.revoke_send`` — removes
    the single ``sender → target`` row in ``comms_grants``. No
    confirmation prompt: the operation is narrow (one row, one
    direction) and idempotent — revoking a non-existent grant prints
    ``no-op`` and exits 0.

    \b
    Example:
      $ sac a2a revoke worker-a worker-b
    """
    if not sender or not target:
        click.echo(
            "error: SENDER and TARGET must both be non-empty",
            err=True,
        )
        raise SystemExit(2)
    from .._state.state_db_nodes import revoke_send
    from ._helpers import console

    removed = revoke_send(sender=sender, target=target)
    if removed:
        console.print(f"[green]ok[/green]  revoked  {sender}  ->  {target}")
    else:
        console.print(f"[dim]no-op[/dim]  no grant  {sender}  ->  {target}")


@click.command("grants")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON array instead of a rich table (scripting-friendly).",
)
def a2a_grants(as_json: bool) -> None:
    """List every row in the ``comms_grants`` table.

    Thin wrapper over ``_state.state_db_nodes.list_comms_grants``.
    Rows are emitted in insertion order with their audit ``note`` (if
    any). Empty table renders as ``(no grants)`` in rich mode and
    ``[]`` in JSON mode.

    \b
    Example:
      $ sac a2a grants
      $ sac a2a grants --json | jq '.[] | select(.sender == "worker-a")'
    """
    from .._state.state_db_nodes import list_comms_grants

    rows = list_comms_grants()
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return
    from ._helpers import console

    if not rows:
        console.print("[dim](no grants)[/dim]")
        return
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("sender")
    table.add_column("target")
    table.add_column("created_at", justify="right")
    table.add_column("note", overflow="fold")
    for r in rows:
        table.add_row(
            str(r["sender"]),
            str(r["target"]),
            f"{r['created_at']:.0f}",
            r["note"] if r["note"] is not None else "",
        )
    console.print(table)


def register(a2a_group) -> None:
    """Attach the five ACL verbs to the parent ``a2a`` Click group."""
    for command in (a2a_grant, a2a_unblock, a2a_block, a2a_revoke, a2a_grants):
        a2a_group.add_command(command)
