"""``sac listen`` — host-level HTTP/JSON control plane for sac agents.

Boots a Starlette app under uvicorn at ``--bind``; routes the
``/v1/sac/agents/...`` and ``/v1/a2a/...`` namespaces from
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


@click.command(name="listen")
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
def listen(
    bind: str,
    token_file: Path | None,
    allow_non_loopback: bool,
    print_token: bool,
) -> None:
    """Boot the sac listen HTTP server.

    \b
    Example:
        sac listen                          # 127.0.0.1:7878
        sac listen --bind 100.64.1.2:7878 --allow-non-loopback
        sac listen --print-token            # echo token then exit
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

    click.echo(f"# sac listen v1 → {host}:{port}", err=True)
    click.echo(f"# token file: {tok_path}", err=True)
    click.echo(f"# health: curl http://{host}:{port}/v1/sac/health", err=True)

    app = create_app(token=token)
    import uvicorn

    # ``ws="none"`` skips uvicorn's websockets backend autodetection —
    # we don't serve WS endpoints, and the WS protocol module imports
    # websockets.legacy which has churned across the websockets package
    # major versions (broken in >=14). Disabling it avoids a startup
    # crash that silently kills a detached ``sac listen``.
    uvicorn.run(app, host=host, port=port, log_level="info", ws="none")
