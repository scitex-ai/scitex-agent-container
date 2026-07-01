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
    # The ONLY registered liveness route is /v1/health (server.py); the
    # old /v1/sac/health string here was a non-route that 404'd.
    click.echo(f"# health: curl http://{host}:{port}/v1/health", err=True)
    click.echo(f"# pidfile: {lock_handle.pid_file}", err=True)

    # ADR-0014 Stage 1 — register the host's operator identity into
    # comms_nodes so cross-host peers can resolve it after a sync.
    # Best-effort: log a warning on failure but never abort startup
    # (a listen that won't bind because of a registry write is worse
    # than a missing federated row).
    _register_self_comms_node(port=port)
    # NOTE: the ``comms_nodes`` peer-sync used to run SYNCHRONOUSLY HERE,
    # before ``uvicorn.run``. That was the live silent-bind-hang vector
    # (INCIDENT 2026-06-26): a single powered-off static peer made its
    # un-timed ssh call hang, blocking boot before 7878 ever bound, with no
    # error logged — the whole fleet lost agent-to-agent comms. The sync now
    # runs best-effort AFTER the bind, off the event loop, as a lifespan
    # task (``_listen._startup_peer_sync.sync_peers_on_listen_startup``, wired
    # in ``_lifecycle._listen_lifespan``). The bind must be impossible to
    # block; nothing that can hang may run on this pre-bind path.

    # Pass the bind port so the lifespan's fail-loud watchdog can probe
    # 127.0.0.1:<port>/v1/health after startup and scream if the daemon
    # comes up but never serves (the silent fleet-comms outage this
    # guards against).
    app = create_app(token=token, health_watchdog_port=port)
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
    """Atomically stop, self-heal, and relaunch the sac listen daemon.

    Deterministic incident recovery (card
    ``sac-listen-restart-selfheal-cli``) — codifies the manual
    ``rm`` pidfile / ``pkill`` wedged remnant / ``setsid sac listen`` /
    ``curl`` dance into one verb. It clears a STALE pidfile (pointing
    at a dead/recycled PID), FORCE-KILLS a wedged process still holding
    the port (the "curl hangs forever" case — even one the pidfile
    never named), then starts and health-probes. No manual shell
    surgery is ever needed.

    FAIL LOUD: if the daemon can't be brought up within the health
    deadline, exits NON-ZERO with an ``ERROR:`` line naming the REAL
    cause (``port still held by PID X`` / ``bind failed``), not a
    generic "did not respond".

    Mirrors ``sac listen``'s own bind resolution: ``sac listen
    restart`` with no args restarts the same daemon ``sac listen``
    with no args would start.

    \b
    Example:
        sac listen restart                  # 10s grace, then SIGKILL
        sac listen restart --grace-secs 30  # longer TERM window
        sac listen restart --force          # SIGKILL daemon + port holder
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

    # Surface any wedged port holder we force-killed so the operator has
    # a paper trail of the self-heal (the codified pkill).
    if result.port_holders_killed:
        pids = ", ".join(str(p) for p in result.port_holders_killed)
        click.echo(
            f"# self-heal: force-killed wedged port holder(s) PID {pids} "
            f"off {host}:{port}",
            err=True,
        )

    if not result.ok:
        raise click.ClickException(
            result.error or "ERROR: sac listen restart failed (unknown reason)"
        )

    msg = f"# sac listen restarted ({'systemd' if result.took_systemd_path else 'direct'}) → {host}:{port}"
    click.echo(msg, err=True)


@listen.command(name="status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON status envelope.",
)
@click.pass_context
def listen_status(ctx: click.Context, as_json: bool) -> None:
    """Report the sac listen daemon's health in one command.

    One-command diagnosis (card ``sac-listen-restart-selfheal-cli``):
    running/down, bound address, pidfile + its PID liveness, and a live
    health-probe result. Exits ``0`` when serving, ``1`` when down or
    wedged — usable interactively and as a scriptable liveness gate.

    \b
    Example:
        sac listen status            # human-readable report
        sac listen status --json     # JSON envelope for scripts
    """
    import json as _json

    from .._listen._restart import (
        HEALTH_PATH,
        pid_alive,
        pidfile_path,
        port_is_bound,
        read_pid_from_file,
    )
    from .._listen._restart import _http_get as _probe_http
    from .._listen._single_instance import default_lock_dir
    from .._listen._status_report import (
        build_status_payload,
        render_status_lines,
    )

    host, port = _split_bind(ctx.obj["bind"])
    pid_file = pidfile_path(port, default_lock_dir())
    pidfile_pid = read_pid_from_file(pid_file)

    payload = build_status_payload(
        host=host,
        port=port,
        pid_file=pid_file,
        pidfile_pid=pidfile_pid,
        pidfile_pid_alive=pidfile_pid is not None and pid_alive(pidfile_pid),
        health_path=HEALTH_PATH,
        http_get=_probe_http,
        port_is_bound=port_is_bound,
    )

    if as_json:
        click.echo(_json.dumps(payload))
    else:
        for line in render_status_lines(payload):
            click.echo(line)

    if not payload["running"]:
        ctx.exit(1)
