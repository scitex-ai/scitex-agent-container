"""ADR-0014 comms_nodes registry hooks for ``sac listen`` boot.

Extracted from :mod:`scitex_agent_container.cli_pkg.listen_cmds` (which
grew past the per-file line cap). These are the two best-effort registry
side-effects the daemon-start path runs — neither may ever block or abort
the bind (a listen that won't bind because of a registry write is worse
than a missing federated row):

* :func:`_register_self_comms_node` — writes this host's operator-identity
  row into ``comms_nodes`` so cross-host peers can resolve it.
* :func:`_maybe_sync_on_start` — the retained-for-legacy synchronous
  startup sync (NO LONGER on the boot path; the live sync now runs off the
  event loop as a lifespan task — see
  :func:`scitex_agent_container._listen._startup_peer_sync.sync_peers_on_listen_startup`).

``listen_cmds`` re-imports both names so the historical import path
``from scitex_agent_container.cli_pkg.listen_cmds import
_register_self_comms_node`` keeps working unchanged. Pure extraction —
no behaviour change.
"""

from __future__ import annotations

import click

__all__ = ["_register_self_comms_node", "_maybe_sync_on_start"]


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


def _maybe_sync_on_start() -> None:
    """ADR-0014 — optionally trigger ``sac registry sync --all`` once.

    Opt-out via the ``comms_nodes.sync_on_start: false`` config flag
    (default True). Best-effort: per-peer failures are logged by the
    sync command itself; we never raise.

    NOT on the boot path anymore. This synchronous helper used to run
    BEFORE ``uvicorn.run`` so the listen had the latest peer view before
    answering inbound A2A POSTs — but an unreachable static peer made its
    ssh call hang and blocked the bind, with no error logged (INCIDENT
    2026-06-26). The startup sync now runs best-effort AFTER the bind, off
    the event loop, as a lifespan task
    (:func:`_listen._startup_peer_sync.sync_peers_on_listen_startup`). This
    helper is retained for explicit/legacy callers only and is bounded by
    an overall budget so even a direct call can never wedge — but
    ``_do_start_listen`` no longer invokes it.
    """
    try:
        from .._state.host_config import load

        cfg = load()
        # The config flag is read by hand because LeadConfig is the
        # only structured block sac currently parses. Look in the raw
        # config dict if present; default True.
        raw_path = cfg.source_path
        sync_on_start = True
        if raw_path is not None and raw_path.is_file():
            import yaml

            raw = yaml.safe_load(raw_path.read_text()) or {}
            comms_nodes_cfg = raw.get("comms_nodes")
            if isinstance(comms_nodes_cfg, dict):
                flag = comms_nodes_cfg.get("sync_on_start", True)
                if isinstance(flag, bool):
                    sync_on_start = flag
        if not sync_on_start:
            return
        # Only run when there is at least one static peer; skip silently
        # otherwise so single-host installs don't spam warnings.
        static_peers = [n for n in cfg.peers.keys() if not any(c in n for c in "*?[")]
        if not static_peers:
            return
        from ._registry_sync import registry_sync_impl

        rc = registry_sync_impl(
            from_peer=None,
            to_peer=None,
            all_peers=True,
            dry_run=False,
            as_json=False,
            # Bound even this legacy/direct path so a re-introduced
            # pre-bind call can never wedge (defense in depth).
            overall_budget_s=60.0,
        )
        if rc != 0:
            click.echo(
                f"# WARN: comms_nodes startup sync had peer failures (rc={rc})",
                err=True,
            )
    except Exception as exc:  # stx-allow: fallback (reason: never block listen on sync)
        click.echo(
            f"# WARN: comms_nodes startup sync failed: {exc!r}",
            err=True,
        )
