"""``sac listen`` — host-level HTTP/JSON control plane for sac agents.

Boots a Starlette app under uvicorn at ``--bind``; routes the
``/agents/...`` and ``/v1/a2a/...`` namespaces from
:mod:`scitex_agent_container._listen.server`.
Token auto-generates at first run; printed once for the operator to
copy. Subsequent runs reuse the token file.

Loopback-only by default; non-loopback binds require the operator to
agree they have an external transport (tunnel / VPN). See
SAC_OROCHI_SCOPES.md §4.4 (orochi owns the cloudflared/autossh mesh —
sac listen should not be reachable from public internet).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import click


def _split_bind(spec: str) -> tuple[str, int]:
    """Split ``host:port`` (or ``[ipv6]:port``) into a tuple."""
    if spec.startswith("["):
        host, _, port = spec[1:].partition("]:")
        return host, int(port)
    host, _, port = spec.rpartition(":")
    if not host or not port:
        raise click.UsageError(f"--bind must be 'host:port', got {spec!r}")
    return host, int(port)


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    host). Hosts without a ``lead:`` block skip this hook quietly —
    those listens serve only sac-managed agents, which already register
    themselves through ``record_instance_start``.
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
            return  # no operator identity configured; nothing to register
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
    """ADR-0014 — optionally trigger ``sac registry sync --all`` once at start.

    Opt-out via the ``comms_nodes.sync_on_start: false`` config flag
    (default True). Best-effort: per-peer failures are logged by the
    sync command itself; we never raise.

    The sync is synchronous so the listen has the latest peer view
    before it starts answering inbound A2A POSTs — that's the closure
    on the bidirectionality bug: a Spartan listen that just came up
    will already know where ``lead`` lives before the first agent on
    Spartan tries to send to it.
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


@click.group(name="listen", invoke_without_command=True)
@click.option(
    "--bind",
    default="127.0.0.1:7878",
    show_default=True,
    help="HOST:PORT to bind. Defaults to loopback per §4.4.",
)
@click.option(
    "--token-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Bearer-token file. Auto-generated at "
        "~/.scitex/agent-container/tokens/listen-<host>.token if missing."
    ),
)
@click.option(
    "--allow-non-loopback",
    is_flag=True,
    default=False,
    help=(
        "Permit binding to non-loopback addresses. Required for "
        "tailscale/tunnel binds; orochi-side mesh is the supported transport."
    ),
)
@click.option(
    "--print-token",
    is_flag=True,
    default=False,
    help="Print the bearer token to stdout and exit.",
)
@click.pass_context
def listen(
    ctx: click.Context,
    bind: str,
    token_file: Path | None,
    allow_non_loopback: bool,
    print_token: bool,
) -> None:
    """Boot the sac listen HTTP server (default) or invoke a subverb.

    \b
    Example:
        sac listen                          # 127.0.0.1:7878 (start)
        sac listen --bind 100.64.1.2:7878 --allow-non-loopback
        sac listen --print-token            # echo token then exit
        sac listen restart                  # atomic stop-clean-relaunch
    """
    # Stash group-level options so subcommands (``restart``) can
    # mirror ``sac listen``'s own bind resolution per design call (a).
    ctx.ensure_object(dict)
    ctx.obj["bind"] = bind
    ctx.obj["token_file"] = token_file
    ctx.obj["allow_non_loopback"] = allow_non_loopback
    ctx.obj["print_token"] = print_token

    if ctx.invoked_subcommand is not None:
        # Subcommand will run next; group callback just stashed options.
        return

    _do_start_listen(
        bind=bind,
        token_file=token_file,
        allow_non_loopback=allow_non_loopback,
        print_token=print_token,
    )


def _do_start_listen(
    *,
    bind: str,
    token_file: Path | None,
    allow_non_loopback: bool,
    print_token: bool,
) -> None:
    """Existing daemon-start logic, extracted so the group callback
    can call it cleanly when no subcommand is given.
    """
    host, port = _split_bind(bind)
    if not _is_loopback(host) and not allow_non_loopback:
        raise click.UsageError(
            f"--bind {host}:{port} is not loopback. Pass --allow-non-loopback "
            "if you have an orochi-style tunnel arranged. See "
            "SAC_OROCHI_SCOPES.md §4.4."
        )

    from .._listen.server import create_app
    from .._listen.tokens import default_token_path, ensure_token

    tok_path = token_file or default_token_path()
    token = ensure_token(tok_path)
    if print_token:
        click.echo(token)
        return

    # Pre-flight import check — surfaces missing/incompatible deps with
    # a clear message BEFORE we print "listening" and detach. Catches
    # the silent-fail trap where ``sac listen`` looked like it bound
    # but actually crashed on a transitive websockets ImportError
    # (we hit this with websockets>=14 dropping legacy.handshake).
    try:
        import uvicorn  # noqa: F401
        from starlette.applications import Starlette  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            f"sac listen requires uvicorn + starlette. Missing: {exc.name}.\n"
            f"Install with: pip install 'scitex-agent-container[listen]'"
        ) from exc

    # Operator task #26 sub (1) — single-instance guard. A second
    # ``sac listen`` while one already holds the port used to crash
    # uvicorn with bare EADDRINUSE + a Python traceback (loud, but the
    # operator had no diagnostic about which process held the port).
    # The flock-backed pidfile guard FAILS LOUD with a structured
    # message naming the holding PID + lock file path so
    # ``kill <pid>`` is actionable without ``lsof``. The flock is
    # kernel-released on process exit (even SIGKILL) so a crashed
    # listen never permanently jams the port. Acquired BEFORE the
    # comms_nodes / startup-sync hooks so a duplicate launch never
    # touches the federated registry — a conflicting second start
    # exits cleanly with no side effects.
    from .._listen._single_instance import (
        ListenAlreadyRunningError,
        acquire_listen_lock,
        default_lock_dir,
        release_listen_lock,
    )

    lock_dir = default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_handle = acquire_listen_lock(port=port, lock_dir=lock_dir)
    except ListenAlreadyRunningError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"# sac listen v1 → {host}:{port}", err=True)
    click.echo(f"# token file: {tok_path}", err=True)
    click.echo(f"# health: curl http://{host}:{port}/v1/sac/health", err=True)
    click.echo(f"# pidfile: {lock_handle.pid_file}", err=True)

    # ADR-0014 Stage 1 — register the host's operator identity into
    # comms_nodes so cross-host peers can resolve it after a sync.
    # Best-effort: log a warning on failure but never abort startup
    # (a listen that won't bind because of a registry write is worse
    # than a missing federated row).
    _register_self_comms_node(port=port)
    _maybe_sync_on_start()

    app = create_app(token=token)
    import uvicorn

    # ``ws="none"`` skips uvicorn's websockets backend autodetection —
    # we don't serve WS endpoints, and the WS protocol module imports
    # websockets.legacy which has churned across the websockets package
    # major versions (broken in >=14). Disabling it avoids a startup
    # crash that silently kills a detached ``sac listen``.
    try:
        uvicorn.run(app, host=host, port=port, log_level="info", ws="none")
    finally:
        # Best-effort release on clean exit; kernel handles dirty
        # exit (SIGKILL / crash / OOM) by closing fds and releasing
        # the flock automatically — the next ``sac listen`` start
        # observes an unlocked file and acquires cleanly.
        release_listen_lock(lock_handle)


# ---------------------------------------------------------------------------
# ``sac listen restart`` — atomic stop-clean-relaunch
# ---------------------------------------------------------------------------


@listen.command(name="restart")
@click.option(
    "--grace-secs",
    type=float,
    default=10.0,
    show_default=True,
    help="SIGTERM-to-SIGKILL escalation deadline (seconds).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip SIGTERM and go straight to SIGKILL.",
)
@click.pass_context
def listen_restart(ctx: click.Context, grace_secs: float, force: bool) -> None:
    """Atomically stop, clean, and relaunch the sac listen daemon.

    Codifies the SIGTERM-hang lockfile recovery sequence documented at
    ``scripts/systemd/README.md`` (PR #294). Mirrors ``sac listen``'s
    own bind resolution: ``sac listen restart`` with no args restarts
    the same daemon ``sac listen`` with no args would start.

    \b
    Example:
        sac listen restart                  # 10s grace, then SIGKILL
        sac listen restart --grace-secs 30  # longer TERM window
        sac listen restart --force          # skip TERM, kill immediately
    """
    from .._listen._restart import (
        format_escalation_warning,
        restart_listen,
    )
    from .._listen._single_instance import default_lock_dir

    bind = ctx.obj["bind"]
    host, port = _split_bind(bind)

    lock_dir = default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)

    result = restart_listen(
        host=host,
        port=port,
        lock_dir=lock_dir,
        grace_secs=grace_secs,
        force=force,
    )

    # Per design call (c): LOUD WARN on SIGKILL escalation, silent on
    # a clean TERM exit. The format is fixed by
    # ``_restart.format_escalation_warning`` (tested + stable).
    if result.escalated_to_sigkill:
        click.echo(format_escalation_warning(grace_secs), err=True)

    if not result.ok:
        raise click.ClickException(
            result.error or "sac listen restart failed (unknown reason)"
        )

    msg = f"# sac listen restarted ({'systemd' if result.took_systemd_path else 'direct'}) → {host}:{port}"
    click.echo(msg, err=True)
