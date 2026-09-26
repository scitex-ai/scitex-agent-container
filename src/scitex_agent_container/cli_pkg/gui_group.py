"""``sac gui {serve,open,status,stop}`` for the scoped Agents dashboard.

The dashboard reads the SAC host control plane (``sac listen``) and projects
it to a browser. This command only manages the *standalone* server; mounting
inside a host (e.g. scitex-hub) is a deployment concern, not a CLI one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import click

from .._django._constants import DEFAULT_PORT
from .._django._server import serve

DEFAULT_HOST = "127.0.0.1"
PACKAGE = "scitex-agent-container"


def _embed():
    try:
        import scitex_app.embed as embed
    except ImportError as exc:  # stx-allow: fallback (reason: clear install hint)
        raise click.ClickException(
            "The SAC GUI requires scitex-app. Install it with: "
            "uv pip install 'scitex-agent-container[gui]'"
        ) from exc
    return embed


def _guard_bind(host: str, allow_remote: bool) -> None:
    if host in {"127.0.0.1", "localhost", "::1"} or allow_remote:
        return
    raise click.ClickException(
        f"Refusing non-loopback bind {host!r}: the dashboard exposes fleet "
        "operational metadata. Pass --allow-remote only behind trusted access "
        "or an authenticated proxy."
    )


def _log_path() -> Path:
    from scitex_config._ecosystem import local_state

    return Path(local_state.runtime_path(PACKAGE, "gui-serve.log"))


@click.group("gui")
def gui_group() -> None:
    """Serve, open, inspect, or stop the scoped Agents dashboard."""


@gui_group.command("serve")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int)
@click.option("--host", default=DEFAULT_HOST, show_default=True)
@click.option("--allow-remote", is_flag=True, help="Allow a non-loopback bind.")
@click.option("--force", is_flag=True, help="Reclaim a previous SAC GUI instance.")
@click.option("--hot-reload", is_flag=True, help="Enable Django auto-reload.")
@click.option("--dry-run", is_flag=True, help="Print the launch without starting it.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def gui_serve(
    port: int,
    host: str,
    allow_remote: bool,
    force: bool,
    hot_reload: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Run the Agents dashboard in the foreground."""
    _guard_bind(host, allow_remote)
    if dry_run:
        payload = {"would_serve": True, "host": host, "port": port, "force": force}
        click.echo(json.dumps(payload) if as_json else f"Would serve at http://{host}:{port}")
        return
    exit_code = serve(
        package=PACKAGE,
        project_dir=os.getcwd(),
        port=port,
        host=host,
        force=force,
        hot_reload=hot_reload,
    )
    if exit_code:
        raise click.exceptions.Exit(exit_code)


@gui_group.command("open")
@click.option("--port", default=DEFAULT_PORT, show_default=True, type=int)
@click.option("--host", default=DEFAULT_HOST, show_default=True)
@click.option("--allow-remote", is_flag=True, help="Allow a non-loopback bind.")
@click.option("--no-browser", is_flag=True, help="Start the server but do not open a tab.")
@click.option("--dry-run", is_flag=True, help="Print the launch without starting it.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def gui_open(
    port: int,
    host: str,
    allow_remote: bool,
    no_browser: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Open the dashboard, starting a detached server when necessary."""
    _guard_bind(host, allow_remote)
    if dry_run:
        payload = {"would_open": True, "host": host, "port": port}
        click.echo(json.dumps(payload) if as_json else f"Would open http://{host}:{port}")
        return

    import webbrowser

    embed = _embed()
    current = embed.gui_status(PACKAGE)
    if not current.get("running"):
        log_path = _log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "scitex_agent_container",
            "gui",
            "serve",
            "--port",
            str(port),
            "--host",
            host,
        ]
        if allow_remote:
            command.append("--allow-remote")
        with log_path.open("ab") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            current = embed.gui_status(PACKAGE)
            if current.get("running"):
                break
            time.sleep(0.2)
        else:
            raise click.ClickException(
                f"Dashboard did not start within 15 seconds; inspect {log_path}"
            )

    if not no_browser:
        webbrowser.open(current["url"])
    click.echo(json.dumps(current) if as_json else f"Dashboard running at {current['url']}")


@gui_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def gui_status(as_json: bool) -> None:
    """Report whether the standalone Agents dashboard is running."""
    current = _embed().gui_status(PACKAGE)
    if as_json:
        click.echo(json.dumps(current))
    elif current.get("running"):
        click.echo(f"running at {current['url']} (pid {current.get('pid')})")
    else:
        click.echo("not running")


@gui_group.command("stop")
@click.option("--dry-run", is_flag=True, help="Print what would be stopped.")
@click.option("--yes", "yes", "-y", is_flag=True, help="Confirm stopping the server.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def gui_stop(dry_run: bool, yes: bool, as_json: bool) -> None:
    """Stop the standalone Agents dashboard."""
    embed = _embed()
    current = embed.gui_status(PACKAGE)
    if not current.get("running"):
        payload = {"running": False, "stopped": False}
        click.echo(json.dumps(payload) if as_json else "not running")
        return
    if dry_run:
        payload = {"would_stop": True, "pid": current.get("pid"), "url": current.get("url")}
        click.echo(json.dumps(payload) if as_json else f"Would stop {current['url']}")
        return
    if not yes:
        raise click.ClickException("Refusing to stop without --yes/-y")
    result = embed.gui_stop(PACKAGE)
    click.echo(json.dumps(result) if as_json else f"stopped (pid {result.get('pid')})")


__all__ = ["gui_group"]
