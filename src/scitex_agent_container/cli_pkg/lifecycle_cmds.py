"""Lifecycle commands: start, stop, restart, cleanup.

Includes the new ``--all`` / ``--force`` flags for bulk-safe operations.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import click

from ..config import AgentConfig, load_config, resolve_config
from ..config._host import resolve_hostname
from ..lifecycle import (
    agent_restart,
    agent_start,
    agent_stop,
    agent_stop_all,
)
from ..registry import Registry
from ._helpers import console

_SKIP_DIR_NAMES = {"legacy-agents", "shared", "GITIGNORED"}


def _singleton_skip_reason(config: AgentConfig, hostname: str) -> str | None:
    """Return a human-readable skip reason if ``config`` is a singleton on
    the wrong host, else None.

    Multi-instance (``hosts:`` set) never skips. Singleton with no host
    preference (empty ``host:``) launches anywhere. Singleton with
    ``host:`` set skips when the current host isn't the preferred (head
    of the list) and isn't a fallback either.
    """
    spec = config.hosts_spec
    if spec.hosts:  # multi-instance
        return None
    host = spec.host
    if not host:  # local singleton — never skip
        return None
    chain = [host] if isinstance(host, str) else list(host)
    if not chain or hostname == chain[0]:
        return None
    if hostname in chain[1:]:
        return None  # we're a fallback for this singleton — let it run
    fallback_str = (
        f" (fallback-hosts: {', '.join(chain[1:])})" if len(chain) > 1 else ""
    )
    return f"singleton prefers '{chain[0]}', current host is '{hostname}'{fallback_str}"


def _iter_agent_yamls(agents_dir: "Path") -> "list[tuple[str, str]]":
    """Yield ``(name, yaml_path)`` for each agent subdir in ``agents_dir``.

    Skips hidden dirs (``.`` / ``_``) and reserved names. Uses the
    ``<agent>/<agent>.yaml`` convention; ``.yml`` is also accepted.
    """
    results: list[tuple[str, str]] = []
    if not agents_dir.exists():
        return results
    for d in sorted(agents_dir.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith((".", "_")):
            continue
        if d.name in _SKIP_DIR_NAMES:
            continue
        for ext in (".yaml", ".yml"):
            candidate = d / f"{d.name}{ext}"
            if candidate.exists():
                results.append((d.name, str(candidate)))
                break
    return results


def _discover_all_agents() -> list[str]:
    """Find all agent YAML files under sac's own root and the plugin-port dirs.

    Search locations (earlier wins on name collision):
      1. ``~/.scitex/agent-container/agents/<name>/<name>.yaml``
         and ``~/.scitex/agent-container/agents/<name>.yaml``.
      2. Each colon-separated dir in ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``.
         External orchestrators (orochi, etc.) set this to hand sac their
         own agent-definition roots without sac knowing about them.

    Returned paths are sorted by effective agent name for stable ordering.
    """
    from pathlib import Path

    from scitex_config._ecosystem import local_state

    # name -> yaml path; later writes are ignored (earlier = higher priority).
    found: dict[str, str] = {}

    primary = local_state.path("agent-container", "agents")
    search_dirs: list[Path] = [primary]

    env_raw = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS", "")
    for p in env_raw.split(":"):
        p = p.strip()
        if p:
            search_dirs.append(Path(p).expanduser())

    for src_dir in search_dirs:
        for name, yaml_path in _iter_agent_yamls(src_dir):
            if name not in found:
                found[name] = yaml_path

    return [found[name] for name in sorted(found)]


@click.command()
@click.argument("config_path", type=str, required=False)
@click.option(
    "--all",
    "start_all",
    is_flag=True,
    default=False,
    help="Start all agents in ~/.scitex/agent-container/agents/ (+ $SCITEX_AGENT_CONTAINER_YAML_DIRS).",
)
@click.option(
    "--no-preflight",
    is_flag=True,
    default=False,
    help="Skip preflight checks (useful for slow SSH hosts).",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="If already running or stale, stop first then start fresh.",
)
@click.option(
    "--resume",
    "resume_id",
    type=str,
    default=None,
    help="Resume a specific Claude Code session by ID (e.g. the UUID of the "
    "*.jsonl under ~/.claude/projects/<encoded>/). Implies --session resume "
    "and overrides the YAML's claude.session / claude.resume_id.",
)
@click.option(
    "--session",
    "session_mode",
    type=click.Choice(
        ["continue-or-new", "continue", "new", "resume"], case_sensitive=False
    ),
    default=None,
    help="Override the YAML's claude.session for this start invocation.",
)
def start(
    config_path: str | None,
    start_all: bool,
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
) -> None:
    """Start an agent from a YAML definition, or --all to start every agent."""
    if (resume_id or session_mode) and start_all:
        click.echo(
            "Error: --resume / --session cannot be combined with --all "
            "(they would apply the same value to every agent).",
            err=True,
        )
        sys.exit(2)
    if resume_id and session_mode and session_mode != "resume":
        click.echo(
            f"Error: --resume requires --session resume, got --session {session_mode}.",
            err=True,
        )
        sys.exit(2)
    if resume_id and session_mode is None:
        session_mode = "resume"
    if start_all:
        yamls = _discover_all_agents()
        if not yamls:
            console.print(
                "[dim]No agents found in "
                "~/.scitex/agent-container/agents/ or $SCITEX_AGENT_CONTAINER_YAML_DIRS[/dim]"
            )
            return
        try:
            current_host = resolve_hostname()
        except RuntimeError:
            current_host = ""
        console.print(f"[blue]Starting {len(yamls)} agents...[/blue]")
        for yaml_path in yamls:
            try:
                config = load_config(yaml_path)
                skip = _singleton_skip_reason(config, current_host)
                if skip:
                    console.print(f"  [yellow]SKIP[/yellow] {config.name}: {skip}")
                    continue
                location = (
                    f"REMOTE: {config.remote.host}"
                    if config.remote.is_remote
                    else "LOCAL"
                )
                console.print(
                    f"  [blue]{config.name}[/blue] ({location})...",
                    end=" ",
                )
                agent_start(yaml_path, no_preflight=no_preflight, force=force)
                console.print("[green]OK[/green]")
            except Exception as exc:
                console.print(f"[red]FAILED: {exc}[/red]")
        return

    if not config_path:
        click.echo(
            "Error: provide a CONFIG_PATH or use --all.\n"
            "  scitex-agent-container start <config.yaml>\n"
            "  scitex-agent-container start --all",
            err=True,
        )
        sys.exit(2)

    try:
        config_path = resolve_config(config_path)
        config = load_config(config_path)
        try:
            current_host = resolve_hostname()
        except RuntimeError:
            current_host = ""
        skip = _singleton_skip_reason(config, current_host)
        if skip:
            console.print(f"[yellow]Skipping '{config.name}': {skip}[/yellow]")
            return
        location = (
            f"REMOTE: {config.remote.host}" if config.remote.is_remote else "LOCAL"
        )
        console.print(
            f"[blue]Starting agent '{config.name}' "
            f"(runtime: {config.runtime}, {location})...[/blue]"
        )
        if no_preflight:
            console.print("[dim]Preflight checks skipped (--no-preflight)[/dim]")
        if force:
            console.print("[dim]Force mode: stopping any existing instance first[/dim]")
        if session_mode:
            console.print(
                f"[dim]Session override: claude.session = {session_mode}"
                + (f", resume_id = {resume_id}" if resume_id else "")
                + "[/dim]"
            )
        agent_start(
            config_path,
            no_preflight=no_preflight,
            force=force,
            session_override=session_mode,
            resume_id_override=resume_id,
        )
        console.print(
            f"[green]Agent '{config.name}' started successfully [{location}][/green]"
        )
        if not config.claude.auto_accept and any(
            df in f
            for f in config.claude.flags
            for df in (
                "--dangerously-skip-permissions",
                "--dangerously-load-development-channels",
            )
        ):
            console.print(
                f"[yellow]auto_accept: false — manual TUI acceptance required on {config.remote.host or 'local'}[/yellow]"
            )
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        traceback.print_exc()
        sys.exit(1)


@click.command()
@click.argument("name", required=False)
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    default=False,
    help="Stop every agent in the registry.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Tolerate stale registry, missing configs, and hook failures.",
)
def stop(name: str | None, stop_all: bool, force: bool) -> None:
    """Stop a running agent (or --all)."""
    if not stop_all and not name:
        click.echo(
            "Error: provide a NAME or use --all.\n"
            "  scitex-agent-container stop <name>\n"
            "  scitex-agent-container stop --all",
            err=True,
        )
        sys.exit(2)

    if stop_all:
        results = agent_stop_all(force=force)
        if not results:
            console.print("[dim]No agents in registry.[/dim]")
            return
        any_failure = False
        for agent_name, ok, msg in results:
            if ok:
                console.print(f"[green]✓ {agent_name}[/green]: {msg}")
            else:
                any_failure = True
                console.print(f"[red]✗ {agent_name}[/red]: {msg}")
        if any_failure and not force:
            sys.exit(1)
        return

    try:
        # Accept either agent name or YAML path
        if "/" in name or name.endswith((".yaml", ".yml")):  # type: ignore[union-attr]
            config_path = resolve_config(name)  # type: ignore[arg-type]
            config = load_config(config_path)
            name = config.name
        agent_stop(name, force=force)  # type: ignore[arg-type]
        console.print(f"[green]Agent '{name}' stopped[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
@click.argument("name")
def restart(name: str) -> None:
    """Restart an agent."""
    try:
        if "/" in name or name.endswith((".yaml", ".yml")):
            config_path = resolve_config(name)
            config = load_config(config_path)
            name = config.name
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
def cleanup() -> None:
    """Remove stale registry entries (where the screen is already gone)."""
    registry = Registry()
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")
