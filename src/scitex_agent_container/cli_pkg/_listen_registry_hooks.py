"""ADR-0014 comms_nodes registry hooks for ``sac listen`` boot.

Extracted from :mod:`scitex_agent_container.cli_pkg.listen_cmds` (which
grew past the per-file line cap). :func:`_register_self_comms_node` is the best-effort registry
side-effect the daemon-start path runs — it may never block or abort the
bind (a listen that won't bind because of a registry write is worse than a
missing federated entry). It writes this host's operator-identity entry
into the ADR-0014 directory so cross-host peers can resolve it.

``_maybe_sync_on_start`` lived here until 2026-08-28. It triggered
``sac registry sync --all``, and that verb is GONE: the directory moved to
the shared PostgreSQL store, so there is no per-host copy to converge and
no peer to pull from. The startup sweep it was retained for
(``_listen._startup_peer_sync``) went with it. Nothing replaces either —
the point of the move is that the peer view is never stale, so there is
nothing to refresh at boot.

``listen_cmds`` re-imports the remaining name so the historical import path
``from scitex_agent_container.cli_pkg.listen_cmds import
_register_self_comms_node`` keeps working unchanged.
"""

from __future__ import annotations

import click

__all__ = ["_register_self_comms_node"]


def _register_self_comms_node(*, port: int) -> None:
    """ADR-0014 — register this listen's operator identity in comms_nodes.

    Best-effort: any failure (no config, no LeadConfig, DB error, name
    collision) is logged to stderr but does NOT prevent ``sac listen``
    from binding. A listen that won't start because of a registry-write
    error is worse than a missing federated row — peers can still
    cross-host-forward to the listen via the existing ``instances``
    table for sac-managed agents; only the operator-identity row needs
    the federated graph.

    Identity source: ``LeadConfig.name`` (e.g. ``lead`` on the lead
    host). Hosts without a ``lead:`` block emit a LOUD WARNING and
    skip — those listens serve only sac-managed agents, which
    register themselves via ``record_instance_start``; operators that
    EXPECT a lead row (cross-host A2A targeting ``lead``) need to
    know the listen isn't writing it. The old silent return was the
    exact bug PR2 (#308) repaired via ``sac registry register``: a
    missing lead block meant ``resolve_node_host('lead')`` returned
    ``None`` fleet-wide with no log line pointing at why. The warning
    + the new repair verb together close the regression door.
    """
    try:
        from .._state.host_config import load
        from .._state.state_db_nodes import (
            CommsNodeConflictError,
            register_comms_node,
        )

        cfg = load()
        lead = cfg.lead
        if lead is None:
            # Loud-but-non-fatal: the listen MUST still bind (a failed
            # bind is worse than a missing federated row), but the
            # operator needs a paper trail when cross-host 'lead'
            # resolution starts failing. The repair path is documented
            # inline so the operator can act without spelunking ADR-0014.
            click.echo(
                "# WARN: comms_nodes self-register skipped — host_config "
                "has no `lead:` block, so this listen will NOT advertise "
                "an operator-identity row. Other hosts' "
                "`resolve_node_host('lead')` will return None until a row "
                "exists. Add a `lead:` block to host_config (preferred) "
                "OR run `sac registry register --name lead --host <h> "
                "--a2a-port <p>` for an immediate no-restart repair.",
                err=True,
            )
            return
        local_host = cfg.canonical_host()
        try:
            register_comms_node(
                name=lead.name,
                host=local_host,
                a2a_port=port,
                source_host=None,  # locally-registered
            )
        except CommsNodeConflictError as exc:
            click.echo(
                f"# WARN: comms_nodes self-register conflict: {exc}",
                err=True,
            )
    except (
        Exception
    ) as exc:  # stx-allow: fallback (reason: never block listen on registry write)
        click.echo(
            f"# WARN: comms_nodes self-register failed: {exc!r}",
            err=True,
        )
