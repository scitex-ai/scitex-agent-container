"""``sac registry register`` — write a comms_nodes row directly.

Operator-repair path for missing federated-graph rows (ADR-0014). The
existing self-register helpers (``sac listen``'s
``_register_self_comms_node``; ``sac mcp channel``'s
``_refresh_comms_node`` task) only run on **process start**. Two real
failure modes the operator cannot fix without this verb:

  1. A long-running ``sac mcp channel --name lead --listen-url ...``
     process predates the self-register feature
     (commit 1f54b4a). Its in-memory code has no
     ``register_comms_node`` call, so until the operator restarts it
     no ``comms_nodes`` row for ``lead`` exists on the lead's host
     and ``resolve_node_host('lead')`` returns ``None`` everywhere.
  2. A host's ``listen`` started before its config gained a
     ``lead:`` block — ``_register_self_comms_node`` saw
     ``cfg.lead is None`` and silently skipped. Re-reading config
     requires a listen restart, which an operator may want to defer.

This verb closes both gaps. The operator runs::

    sac registry register --name lead --host lead-host --a2a-port 7878

and the record lands in ``comms_nodes`` immediately. Since 2026-08-28
that store is one shared PostgreSQL, so EVERY host sees it at once and
there is nothing to pull — the ``sac registry sync --from`` step this
paragraph used to prescribe is obsolete. No process restart required.

Failure policy — **fail loud**, not best-effort. ``sac listen`` and
``sac mcp channel`` swallow registry-write failures with
``log.warning`` because their MCP / SSE handshakes are the primary
job and a missed row can be re-tried next tick. This verb's primary
job IS the registry write, invoked by a human; a silent
:class:`CommsNodeConflictError` would defeat the operator's intent.
We surface the conflict via ``click.ClickException`` (exit 1) with the
underlying error message so the operator can ``--unregister`` /
re-typed-args their way out.
"""

from __future__ import annotations

import click

from .._state.state_db_nodes import (
    CommsNodeConflictError,
    register_comms_node,
)


@click.command("register")
@click.option(
    "--name",
    required=True,
    type=str,
    help="Node name to register (e.g. 'lead', 'spartan-listen').",
)
@click.option(
    "--host",
    required=True,
    type=str,
    help="Canonical host the node lives on (matches the host's "
    "host_config.canonical_host()).",
)
@click.option(
    "--a2a-port",
    "a2a_port",
    required=True,
    type=int,
    help="TCP port the node's a2a HTTP endpoint listens on.",
)
@click.option(
    "--source-host",
    "source_host",
    type=str,
    default=None,
    help=(
        "Source-of-record host. Defaults to None — the row is treated "
        "as locally-registered (matches what `sac listen` and `sac mcp "
        "channel` self-register write). Set this only when relaying a "
        "peer's record from a third host. Rare, and rarer still since "
        "the store became fleet-shared: it now only marks the record as "
        "not-locally-owned for the conflict check."
    ),
)
def registry_register(
    name: str,
    host: str,
    a2a_port: int,
    source_host: str | None,
) -> None:
    """Write a ``comms_nodes`` row directly. ADR-0014 operator-repair path.

    Use cases:

      * The lead's row never landed (the lead's mcp channel process
        predates the self-register feature) — write it manually so other
        agents resolve ``lead`` immediately. Restart the lead's channel
        at leisure to pick up the heartbeat refresh.
      * A peer host's listen started before its config gained a
        ``lead:`` block — register the row manually without restarting
        the listen.

    Fail-loud on conflict (different existing host/port from a
    different source): exits 1 with the conflict message so the
    operator can resolve it explicitly. Silent overwrite is the wrong
    default for a verb the human typed.

    \\b
    Examples:
      $ sac registry register --name lead --host lead-host --a2a-port 7878
      $ sac registry register --name spartan-2 --host spartan-2.lan --a2a-port 7878
    """
    try:
        register_comms_node(
            name=name,
            host=host,
            a2a_port=a2a_port,
            source_host=source_host,
        )
    except CommsNodeConflictError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        # Bad inputs (empty name/host, non-positive port). Click's own
        # validators catch most, but register_comms_node enforces too.
        raise click.UsageError(str(exc)) from exc
    click.echo(
        f"registered comms_nodes: name={name!r} host={host!r} "
        f"a2a_port={a2a_port} source_host={source_host!r}"
    )


__all__ = ["registry_register"]
