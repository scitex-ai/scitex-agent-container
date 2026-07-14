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


# ADR-0014 comms_nodes registry hooks — extracted to a sibling module to
# keep this file under the per-file line cap. Re-exported here so the
# historical import path
# ``from scitex_agent_container.cli_pkg.listen_cmds import
# _register_self_comms_node`` (used by tests + the boot path below)
# keeps working unchanged.
from ._listen_registry_hooks import (  # noqa: E402
    _maybe_sync_on_start,  # noqa: F401  (re-exported for tests / legacy callers)
    _register_self_comms_node,
)

# ---------------------------------------------------------------------------
# Bare-boot deprecation — scitex CLI convention §5, phase W (warn + forward)
# ---------------------------------------------------------------------------

#: The release that REMOVES boot-on-bare-``sac listen``. Every warning names
#: it, so callers always know the deadline (§5: "each phase names the removal
#: version").
BARE_BOOT_REMOVAL_VERSION = "v0.23.0"


def _warn_bare_boot_deprecated() -> None:
    """Warn that bare ``sac listen`` booted a daemon, and name the verb.

    Phase W: the bare form still FORWARDS to the boot, so every caller keeps
    working — and three still invoke it bare TODAY (the systemd unit's
    ``ExecStart``, ``_listen._restart``'s direct-spawn argv, and the systemd
    JobSpec in PR #543). ``sac listen`` IS the host control plane; flipping
    the bare form to show-help would take the fleet's host access down with
    it. Removing the bare boot is a FOLLOW-UP, only after all three migrate.

    DELIBERATE DEVIATION from §5a's once-per-shell suppression: §5a keys its
    marker on ``$PPID`` to give one warning per interactive shell, but this
    daemon's principal caller is a **systemd unit**, whose PPID is the
    (constant) user manager — a once-per-PPID marker would warn on the first
    boot then stay silent forever, muting exactly the caller that must
    migrate. §5a's rationale ("cron jobs and loops would drown in it") also
    does not apply: a daemon boots once per lifetime, so this is at most one
    journal line per start. stderr only, and no marker-file write — the
    control plane's boot path must not be able to fail on a write it does
    not need.
    """
    click.echo(
        "WARN: bare `sac listen` is DEPRECATED — use `sac listen start` "
        f"(removed in {BARE_BOOT_REMOVAL_VERSION}).",
        err=True,
    )
    click.echo(
        "      `listen` is a command GROUP, not a verb: booting a daemon off "
        "the bare noun means a typo or a stray tab-complete starts a server. "
        "Verbs: start / stop / restart / status  (see `sac listen -h`).",
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
    """The host HTTP/JSON control plane (command group).

    The plane every sac agent reaches the host through — 127.0.0.1:7878 by
    default, bearer-token authenticated, loopback-only unless you opt out.
    `listen` is a NOUN; the verbs below are its whole lifecycle.

    \b
    Example:
        sac listen start                    # boot the daemon (loopback only)
        sac listen status                   # health report; exit 1 if down
        sac listen status --json            # machine-readable envelope
        sac listen stop                     # stop it (idempotent)
        sac listen restart                  # atomic stop-clean-relaunch
        sac listen start --print-token      # echo the bearer token, then exit

    \b
    DEPRECATED: bare `sac listen` still BOOTS the daemon, so the systemd unit
    and every existing launcher keep working — but it now warns. Use
    `sac listen start`. The bare form is removed in v0.23.0.
    """
    # Stash group-level options so subcommands (``start`` / ``stop`` /
    # ``restart`` / ``status``) can mirror ``sac listen``'s own bind
    # resolution per design call (a).
    ctx.ensure_object(dict)
    ctx.obj["bind"] = bind
    ctx.obj["token_file"] = token_file
    ctx.obj["allow_non_loopback"] = allow_non_loopback
    ctx.obj["print_token"] = print_token

    if ctx.invoked_subcommand is not None:
        # Subcommand will run next; group callback just stashed options.
        return

    # Bare ``sac listen`` — the deprecated boot-by-default path (phase W:
    # warn + FORWARD). ``--print-token`` short-circuits inside
    # ``_do_start_listen`` before anything binds, so it is NOT a boot and
    # must not draw a boot-deprecation warning.
    if not print_token:
        _warn_bare_boot_deprecated()

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

    # Hot-standby + failover (card ``sac-listen-hot-standby-no-crashloop``).
    # The flock-backed pidfile guard (operator task #26 sub (1)) remains
    # the ATOMIC bind arbiter — at most one process holds it, so two
    # instances can never both bind. But a second ``sac listen`` launched
    # while one already holds the port used to turn that contention into
    # ``ListenAlreadyRunningError`` → ``click.ClickException`` → exit 1,
    # which under the unit's ``Restart=always`` was an INFINITE
    # CRASH-LOOP (NRestarts 11→34+, ~4s CPU/cycle, wedged the systemd
    # user manager). ``resolve_startup`` fixes the root cause: on
    # contention it does NOT exit — it STANDS BY as a warm spare
    # (health-checking the holder) and FAILS OVER the instant the holder
    # dies or wedges. Runs BEFORE the comms_nodes / startup-sync hooks so
    # a standing-by duplicate never touches the federated registry. The
    # signal guard makes a SIGTERM during standby a prompt CLEAN exit
    # (``systemctl stop`` works) and restores the prior handlers on exit
    # so uvicorn installs its own graceful-shutdown handlers below.
    from .._listen._single_instance import (
        default_lock_dir,
        release_listen_lock,
    )
    from .._listen._standby import resolve_startup, standby_signal_guard

    lock_dir = default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    with standby_signal_guard():
        lock_handle = resolve_startup(host=host, port=port, lock_dir=lock_dir)
    if lock_handle is None:
        # A stop signal (``systemctl stop`` / Ctrl-C) arrived while
        # standing by. We never acquired the lock — nothing to release;
        # exit cleanly without binding.
        click.echo(
            "# sac listen: received shutdown signal while standing by "
            "— exiting without binding",
            err=True,
        )
        return

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
    import os

    import uvicorn

    # Bounded graceful-shutdown timeout. uvicorn's default is ``None`` →
    # on SIGTERM it waits FOREVER for in-flight requests to finish, and a
    # long-lived SSE inbox stream parked on ``queue.get()`` never does,
    # so the daemon hangs until ``sac listen restart --force`` escalates
    # to SIGKILL after 10 s (card sac-listen-sigterm-sse-shutdown-hang).
    # A bounded timeout guarantees uvicorn force-cancels a stuck stream
    # well within that grace; the lifespan's shutdown bridge (which
    # closes the inbox broker the instant ``should_exit`` flips) makes
    # the common case exit promptly, long before this floor is reached.
    # Override via SAC_LISTEN_SHUTDOWN_GRACE_S.
    try:
        _grace_shutdown = float(os.environ.get("SAC_LISTEN_SHUTDOWN_GRACE_S", "5"))
    except (TypeError, ValueError):
        _grace_shutdown = 5.0

    # ``ws="none"`` skips uvicorn's websockets backend autodetection —
    # we don't serve WS endpoints, and the WS protocol module imports
    # websockets.legacy which has churned across the websockets package
    # major versions (broken in >=14). Disabling it avoids a startup
    # crash that silently kills a detached ``sac listen``.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        ws="none",
        timeout_graceful_shutdown=_grace_shutdown,
    )
    server = uvicorn.Server(config)
    # The lifespan's shutdown bridge reads this to detect uvicorn's
    # ``should_exit`` (set synchronously by its SIGTERM handler) and
    # close the inbox broker promptly, so in-flight SSE streams return
    # at once instead of blocking the graceful shutdown.
    app.state.uvicorn_server = server
    try:
        server.run()
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


# ---------------------------------------------------------------------------
# ``sac listen start`` / ``sac listen stop`` — the explicit lifecycle verbs.
#
# Defined in a sibling module to keep THIS file under the 512-line cap (the
# same extraction ``_listen_registry_hooks`` made) and attached here so they
# resolve on the group. ``_listen_verbs`` imports its boot primitives lazily,
# inside the command bodies, so importing it here is not a cycle.
# ---------------------------------------------------------------------------

from ._listen_verbs import listen_start, listen_stop  # noqa: E402

listen.add_command(listen_start)
listen.add_command(listen_stop)
