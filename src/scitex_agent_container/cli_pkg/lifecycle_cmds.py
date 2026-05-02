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

    Multi-instance (``hosts:`` set or per-host scheduling) never skips.
    Singleton with no host preference launches anywhere. Singleton with
    ``host:``/``preferred-host`` set skips when the current host doesn't match.
    """
    # v3 config: use hosts_spec
    spec = config.hosts_spec
    if spec.hosts:  # multi-instance
        return None
    host = spec.host
    if host:
        chain = [host] if isinstance(host, str) else list(host)
        if not chain or hostname == chain[0]:
            return None
        if hostname in chain[1:]:
            return None
        fallback_str = (
            f" (fallback-hosts: {', '.join(chain[1:])})" if len(chain) > 1 else ""
        )
        return f"singleton prefers '{chain[0]}', current host is '{hostname}'{fallback_str}"
    # v2 config: use scheduling spec
    sched = config.scheduling
    if sched.mode != "singleton":
        return None
    if not sched.preferred_host:
        return None
    if sched.preferred_host == hostname:
        return None
    fallback = (
        f" (fallback-hosts: {', '.join(sched.fallback_hosts)})"
        if sched.fallback_hosts
        else ""
    )
    return (
        f"singleton pinned to '{sched.preferred_host}', "
        f"current host is '{hostname}'{fallback}"
    )


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
    """Find all agent YAML files in the sac install root and shared-host layout.

    Search locations (earlier wins on name collision):
      1. ``~/.scitex/agent-container/agents/<name>/<name>.yaml`` (sac root)
      2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (plugin-port colon-separated dirs)
      3. ``~/.scitex/orochi/<HOST>/agents/`` (host-specific override)
      4. ``~/.scitex/orochi/shared/agents/`` (fleet-shared)
      5. ``~/.scitex/orochi/agents/`` (legacy flat layout)

    Note: per-agent runtime state (CLAUDE.md / .mcp.json / .claude/) lives at
    ``~/.scitex/orochi/runtime/workspaces/<effective-id>/`` (see the 2026-04-17
    layout). ``<HOST> = ${SCITEX_OROCHI_HOSTNAME:-$(hostname -s)}``.

    Returned paths are sorted by effective agent name for stable ordering.
    """
    from pathlib import Path

    # name -> yaml path; later writes are ignored (earlier = higher priority).
    found: dict[str, str] = {}

    home = Path.home()
    primary = home / ".scitex" / "agent-container" / "agents"
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

    # Fleet layout search
    root = home / ".scitex" / "orochi"
    try:
        host = resolve_hostname()
    except RuntimeError:
        host = ""

    host_dir = root / host / "agents" if host else None
    shared_dir = root / "shared" / "agents"

    for src_dir in (host_dir, shared_dir):
        if src_dir is None:
            continue
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
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Materialize the workspace files (CLAUDE.md, .mcp.json, .env, "
    "settings.json) but skip launching the multiplexer / Claude Code. "
    "Use to inspect the planned workspace without starting the agent.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON report on stdout instead of human prose.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (currently a no-op; reserved for future "
    "interactive confirmations).",
)
def start(
    config_path: str | None,
    start_all: bool,
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
    dry_run: bool,
    as_json: bool,
    yes: bool,
) -> None:
    """Start an agent from a YAML definition, or --all to start every agent.

    \b
    Example:
      $ sac start ~/.scitex/agent-container/agents/foo/foo.yaml
      $ sac start --all
      $ sac start foo --dry-run
    """
    import json as _json

    def _emit_json(payload: dict) -> None:
        click.echo(_json.dumps(payload, ensure_ascii=False))

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
        if not yes and not click.confirm(f"Start {len(yamls)} agents?", default=True):
            click.echo("Aborted.")
            return
        try:
            current_host = resolve_hostname()
        except (
            RuntimeError
        ):  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            current_host = ""
        console.print(f"[blue]Starting {len(yamls)} agents...[/blue]")
        for yaml_path in yamls:
            # stx-allow: fallback (reason: one agent's config parse or launch failure must not abort the remaining agents in a bulk --all start; printing FAILED and continuing is the correct bulk-safe behavior)
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
                agent_start(
                    yaml_path,
                    no_preflight=no_preflight,
                    force=force,
                    dry_run=dry_run,
                )
                console.print(
                    "[green]DRY-RUN OK[/green]" if dry_run else "[green]OK[/green]"
                )
            except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
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

    # stx-allow: fallback (reason: config resolution, YAML parse, or agent_start can raise on misconfiguration or launch failure; catching here gives a clean error message and non-zero exit rather than an unhandled traceback)
    try:
        config_path = resolve_config(config_path)
        config = load_config(config_path)
        try:
            current_host = resolve_hostname()
        except (
            RuntimeError
        ):  # stx-allow: fallback (reason: runtime state error — handled gracefully)
            current_host = ""
        skip = _singleton_skip_reason(config, current_host)
        if skip:
            if as_json:
                _emit_json(
                    {
                        "name": config.name,
                        "status": "skipped",
                        "reason": skip,
                        "dry_run": dry_run,
                    }
                )
            else:
                console.print(f"[yellow]Skipping '{config.name}': {skip}[/yellow]")
            return
        location = (
            f"REMOTE: {config.remote.host}" if config.remote.is_remote else "LOCAL"
        )
        if not as_json:
            console.print(
                f"[blue]{'Dry-running' if dry_run else 'Starting'} agent "
                f"'{config.name}' (runtime: {config.runtime}, {location})...[/blue]"
            )
            if no_preflight:
                console.print("[dim]Preflight checks skipped (--no-preflight)[/dim]")
            if force:
                console.print(
                    "[dim]Force mode: stopping any existing instance first[/dim]"
                )
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
            dry_run=dry_run,
            session_override=session_mode,
            resume_id_override=resume_id,
        )
        if as_json:
            _emit_json(
                {
                    "name": config.name,
                    "status": "dry_run_ok" if dry_run else "started",
                    "runtime": config.runtime,
                    "location": location.lower(),
                    "workdir": config.expanded_workdir,
                    "dry_run": dry_run,
                }
            )
        else:
            verb = (
                "dry-run prepared the workspace for"
                if dry_run
                else "started successfully ["
            )
            tail = "" if dry_run else f"[{location}]"
            console.print(
                f"[green]Agent '{config.name}' {verb}{tail}[/green]"
                if dry_run
                else f"[green]Agent '{config.name}' started successfully [{location}][/green]"
            )
            if (
                not dry_run
                and not config.claude.auto_accept
                and any(
                    df in f
                    for f in config.claude.flags
                    for df in (
                        "--dangerously-skip-permissions",
                        "--dangerously-load-development-channels",
                    )
                )
            ):
                console.print(
                    f"[yellow]auto_accept: false — manual TUI acceptance required on {config.remote.host or 'local'}[/yellow]"
                )
    except Exception as exc:
        if as_json:
            _emit_json(
                {
                    "name": config_path,
                    "status": "error",
                    "error": str(exc),
                    "dry_run": dry_run,
                }
            )
        else:
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
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print which agent(s) would be stopped without sending the kill.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (only used by --all to confirm bulk stop).",
)
def stop(
    name: str | None, stop_all: bool, force: bool, dry_run: bool, yes: bool
) -> None:
    """Stop a running agent (or --all).

    \b
    Example:
      $ sac stop foo
      $ sac stop --all
      $ sac stop foo --dry-run
    """
    if not stop_all and not name:
        click.echo(
            "Error: provide a NAME or use --all.\n"
            "  scitex-agent-container stop <name>\n"
            "  scitex-agent-container stop --all",
            err=True,
        )
        sys.exit(2)

    if dry_run:
        target = "all registered agents" if stop_all else f"agent '{name}'"
        click.echo(f"[dry-run] would stop {target}")
        return

    if stop_all:
        if not yes and not click.confirm("Stop ALL registered agents?", default=True):
            click.echo("Aborted.")
            return
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

    # stx-allow: fallback (reason: config resolution or agent_stop can raise if the agent is not in the registry or the session is already gone; error message + sys.exit(1) is cleaner than an unhandled traceback)
    try:
        # Accept either agent name or YAML path
        if "/" in name or name.endswith((".yaml", ".yml")):  # type: ignore[union-attr]
            config_path = resolve_config(name)  # type: ignore[arg-type]
            config = load_config(config_path)
            name = config.name
        agent_stop(name, force=force)  # type: ignore[arg-type]
        console.print(f"[green]Agent '{name}' stopped[/green]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
@click.argument("name")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be restarted without making changes.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def restart(name: str, dry_run: bool, yes: bool) -> None:
    """Restart an agent.

    \b
    Example:
      $ sac restart foo
      $ sac restart foo --dry-run
    """
    if dry_run:
        click.echo(f"[dry-run] would restart agent '{name}'")
        return
    if not yes and not click.confirm(f"Restart agent '{name}'?", default=True):
        click.echo("Aborted.")
        return
    # stx-allow: fallback (reason: config resolution or agent_restart can raise if the agent is not running or the session cannot be found; error message + sys.exit(1) is cleaner than an unhandled traceback)
    try:
        if "/" in name or name.endswith((".yaml", ".yml")):
            config_path = resolve_config(name)
            config = load_config(config_path)
            name = config.name
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show how many stale entries would be removed without modifying registry.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def cleanup(dry_run: bool, yes: bool) -> None:
    """Remove stale registry entries (where the screen is already gone).

    \b
    Example:
      $ sac cleanup
      $ sac cleanup --dry-run
    """
    registry = Registry()
    if dry_run:
        # Probe count without mutating: re-implement minimal stale check via
        # a fresh probe — fall back to a textual hint if the registry doesn't
        # expose a non-mutating preview.
        click.echo(
            "[dry-run] would remove stale registry entries (run without --dry-run to apply)"
        )
        return
    if not yes and not click.confirm("Remove stale registry entries?", default=True):
        click.echo("Aborted.")
        return
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")


# ---------------------------------------------------------------------------
# Auto-accept subcommands
# ---------------------------------------------------------------------------


@click.command("send-accept")
@click.argument("agent")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be sent without dispatching the action.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (currently a no-op; reserved).",
)
def send_accept(agent: str, dry_run: bool, yes: bool) -> None:
    """One-shot: capture → classify → respond for AGENT, then exit.

    \b
    Example:
      $ sac send-accept foo
      $ sac send-accept foo --dry-run
    """
    _ = yes  # accepted for API consistency; no prompt is currently shown.
    if dry_run:
        click.echo(f"[dry-run] would send accept action for agent '{agent}'")
        return
    from ..auto_accept_daemon import send_accept_once

    state, sent = send_accept_once(agent)
    if sent:
        console.print(f"[green]sent action for agent '{agent}' (state={state})[/green]")
    else:
        console.print(f"[dim]no action for agent '{agent}' (state={state})[/dim]")


@click.command("start-auto-accept")
@click.argument("agent")
@click.option(
    "--tick",
    "tick_s",
    default=60.0,
    show_default=True,
    help="Daemon tick interval in seconds.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be started without forking the daemon.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (currently a no-op; reserved).",
)
def start_auto_accept(agent: str, tick_s: float, dry_run: bool, yes: bool) -> None:
    """Start the auto-accept daemon for AGENT (throttle 5 s min, default 60 s tick).

    \b
    Example:
      $ sac start-auto-accept foo
      $ sac start-auto-accept foo --tick 30
    """
    _ = yes  # accepted for API consistency; no prompt is currently shown.
    if dry_run:
        click.echo(
            f"[dry-run] would start auto-accept daemon for agent '{agent}' (tick={tick_s}s)"
        )
        return
    import multiprocessing

    from ..auto_accept_daemon import read_pid, run_daemon

    existing = read_pid(agent)
    if existing:
        try:
            import os as _os

            _os.kill(existing, 0)
            console.print(
                f"[yellow]auto-accept daemon already running for '{agent}' (pid={existing})[/yellow]"
            )
            return
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            pass  # stale pid file

    def _target():
        run_daemon(agent, tick_s=tick_s)

    p = multiprocessing.Process(
        target=_target, daemon=False, name=f"sac-auto-accept-{agent}"
    )
    p.start()
    console.print(
        f"[green]auto-accept daemon started for '{agent}' (pid={p.pid}, tick={tick_s}s)[/green]"
    )


@click.command("stop-auto-accept")
@click.argument("agent")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be stopped without sending SIGTERM.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def stop_auto_accept(agent: str, dry_run: bool, yes: bool) -> None:
    """Stop the auto-accept daemon for AGENT.

    \b
    Example:
      $ sac stop-auto-accept foo
      $ sac stop-auto-accept foo --dry-run
    """
    if dry_run:
        click.echo(f"[dry-run] would stop auto-accept daemon for agent '{agent}'")
        return
    if not yes and not click.confirm(
        f"Stop auto-accept daemon for '{agent}'?", default=True
    ):
        click.echo("Aborted.")
        return
    import os as _os
    import signal

    from ..auto_accept_daemon import clear_pid, read_pid

    pid = read_pid(agent)
    if pid is None:
        console.print(f"[dim]no auto-accept daemon found for '{agent}'[/dim]")
        return
    try:
        _os.kill(pid, signal.SIGTERM)
        console.print(
            f"[green]sent SIGTERM to auto-accept daemon for '{agent}' (pid={pid})[/green]"
        )
    except (
        ProcessLookupError
    ):  # stx-allow: fallback (reason: process probe expected failure)
        console.print(f"[yellow]stale pid {pid} for '{agent}' — cleaning up[/yellow]")
        clear_pid(agent)
