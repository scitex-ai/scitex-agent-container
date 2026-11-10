"""Explicit listener start/stop commands attached by ``listen_cmds``.

Startup runs in the foreground under the caller's supervisor. Stop uses the
same lifecycle backend as restart. Bare ``sac listen`` retains startup
compatibility for existing launchers; removing it requires their migration.

Runtime imports stay inside callbacks to avoid the registration cycle and
keep cold startup cheap. ``--dry-run`` returns an argument-only plan before
lifecycle work, including when ``--print-token`` is requested.
"""

from __future__ import annotations

from pathlib import Path

import click

#: Mirrors the ``listen`` group's own ``--bind`` default. Used only when a
#: verb runs with no group context at all (e.g. ``CliRunner`` invoking the
#: command object directly); the group normally stashes the resolved value.
DEFAULT_BIND = "127.0.0.1:7878"


@click.command(name="start")
@click.option(
    "--bind",
    default=None,
    help=f"HOST:PORT to bind. Loopback-only per §4.4.  [default: {DEFAULT_BIND}]",
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
        "tailscale/tunnel binds; an external mesh is the supported transport."
    ),
)
@click.option(
    "--print-token",
    is_flag=True,
    default=False,
    help="Print the bearer token to stdout and exit (does not boot).",
)
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments as JSON without starting."
)
@click.pass_context
def listen_start(
    ctx: click.Context,
    bind: str | None,
    token_file: Path | None,
    allow_non_loopback: bool,
    print_token: bool,
    dry_run: bool = False,
) -> None:
    """Boot the sac listen HTTP/JSON control-plane daemon.

    The explicit form of what bare `sac listen` still does today. Binds
    loopback-only unless --allow-non-loopback is passed, and writes a
    flock-backed pidfile that `stop` / `restart` / `status` address.

    Runs in the FOREGROUND (systemd's Type=simple supervises it; `sac listen
    restart` re-spawns it under setsid). If another instance already holds
    the port, this one does NOT crash — it stands by as a warm spare and
    fails over the moment the holder dies.

    Options may be given on the verb (`sac listen start --bind ...`) or on
    the group (`sac listen --bind ... start`); the verb wins.

    \b
    Example:
        sac listen start                                  # 127.0.0.1:7878
        sac listen start --bind 127.0.0.1:7979            # custom port
        sac listen start --bind 100.64.1.2:7878 --allow-non-loopback
        sac listen start --print-token                    # echo token, exit
        sac listen start --dry-run                        # argument preview

    \b
    Exit codes:
        0  argument preview, clean shutdown or --print-token printed
        1  uvicorn/starlette missing, or the bind failed
        2  non-loopback --bind without --allow-non-loopback
    """
    from .listen_cmds import _do_start_listen

    obj = ctx.obj or {}
    if dry_run:
        from ._listen_preview import show_listen_preview

        show_listen_preview(
            "start",
            bind=bind or obj.get("bind") or DEFAULT_BIND,
            token_file=token_file or obj.get("token_file"),
            allow_non_loopback=allow_non_loopback
            or bool(obj.get("allow_non_loopback")),
            print_token=print_token or bool(obj.get("print_token")),
        )
        return
    _do_start_listen(
        bind=bind or obj.get("bind") or DEFAULT_BIND,
        token_file=token_file or obj.get("token_file"),
        allow_non_loopback=allow_non_loopback or bool(obj.get("allow_non_loopback")),
        print_token=print_token or bool(obj.get("print_token")),
    )


@click.command(name="stop")
@click.option(
    "--grace-secs",
    type=float,
    default=10.0,
    show_default=True,
    help="SIGTERM-to-SIGKILL escalation deadline (seconds).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON result envelope on stdout.",
)
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments as JSON without stopping."
)
@click.option("-y", "--yes", is_flag=True, help="Approve stopping the listener.")
@click.pass_context
def listen_stop(
    ctx: click.Context,
    grace_secs: float,
    as_json: bool,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Stop the running sac listen daemon.

    The stop half of `restart`, on its own: SIGTERM the pidfile's PID, poll
    to --grace-secs, escalate to SIGKILL, verify it is REALLY dead before
    clearing the pidfile, then force-kill any wedged remnant still holding
    the port that the pidfile never named (the "curl hangs forever" case).
    Shares ONE implementation with `restart`, so the two cannot drift.

    IDEMPOTENT: stopping an already-stopped daemon exits 0 and reports "not
    running" — the same contract as `systemctl stop`.

    Mirrors `sac listen`'s own bind resolution: `sac listen stop` stops the
    daemon `sac listen start` would have started.

    \b
    Example:
        sac listen stop --yes                  # SIGTERM, 10s grace, then SIGKILL
        sac listen stop --yes                  # stop the daemon
        sac listen stop --yes --json           # machine-readable envelope
        sac listen stop --dry-run        # argument preview

    \b
    Exit codes:
        0  argument preview, stopped or already down (idempotent)
        1  could not stop (PID survived SIGKILL / port still wedged)
        2  missing --yes/-y approval
    """
    if dry_run:
        from ._listen_preview import show_listen_preview

        show_listen_preview(
            "stop",
            bind=(ctx.obj or {}).get("bind") or DEFAULT_BIND,
            grace_secs=grace_secs,
        )
        return
    from ._listen_stop_approval import require_listener_stop_approval

    require_listener_stop_approval(yes)
    force = False
    import json as _json

    from .._listen._restart import format_escalation_warning
    from .._listen._single_instance import default_lock_dir
    from .._listen._stop import stop_listen
    from .listen_cmds import _split_bind

    host, port = _split_bind((ctx.obj or {}).get("bind") or DEFAULT_BIND)

    lock_dir = default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)

    result = stop_listen(
        host=host,
        port=port,
        lock_dir=lock_dir,
        grace_secs=grace_secs,
        force=force,
    )

    # LOUD WARN on SIGKILL escalation, silent on a clean TERM exit — the
    # same design call (c) ``restart`` follows, using the same formatter.
    if result.escalated_to_sigkill:
        click.echo(format_escalation_warning(grace_secs), err=True)

    # Paper trail for the codified pkill (the wedged, untracked remnant).
    if result.port_holders_killed:
        pids = ", ".join(str(p) for p in result.port_holders_killed)
        click.echo(
            f"# self-heal: force-killed wedged port holder(s) PID {pids} "
            f"off {host}:{port}",
            err=True,
        )

    # §8: data on stdout, logs/warnings on stderr — `… --json | jq` must be
    # uncontaminated even on the failure path below.
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": result.ok,
                    "host": host,
                    "port": port,
                    "was_running": result.was_running,
                    "prior_pid": result.prior_pid,
                    "escalated_to_sigkill": result.escalated_to_sigkill,
                    "port_holders_killed": list(result.port_holders_killed),
                    "error": result.error,
                }
            )
        )

    # FAIL LOUD: the error names the REAL cause (``PID X survived SIGKILL`` /
    # ``port still held``), never a generic "did not stop".
    if not result.ok:
        raise click.ClickException(
            result.error or "ERROR: sac listen stop failed (unknown reason)"
        )

    if result.was_running:
        pid_note = f" (PID {result.prior_pid})" if result.prior_pid else ""
        click.echo(f"# sac listen stopped → {host}:{port}{pid_note}", err=True)
    else:
        click.echo(
            f"# sac listen was not running → {host}:{port} (nothing to stop)",
            err=True,
        )


__all__ = ["DEFAULT_BIND", "listen_start", "listen_stop"]
