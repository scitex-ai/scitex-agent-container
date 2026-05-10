"""Lifecycle commands: start, stop, restart, cleanup.

Includes the new ``--all`` / ``--force`` flags for bulk-safe operations.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import click

from .._lifecycle.lifecycle import (
    agent_restart,
    agent_start,
    agent_stop,
)
from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ..config._host import resolve_hostname
from ..config._resolve import resolve_with_prefix
from ._helpers import agent_name_complete, console

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
    """Find all agent YAML files via sac's standard search chain.

    Search locations (earlier wins on name collision):
      0. **Project-local** — first ``.scitex/agent-container/agents/``
         found walking upward from cwd. Highest priority so checked-in
         test agents and CI fixtures override globals.
      1. ``~/.scitex/agent-container/agents/<name>/spec.yaml`` — sac's own root.
      2. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — plugin port (colon-separated)
         for downstream orchestrators (e.g. orochi) to inject extra paths
         without sac knowing about them.

    sac is standalone: it never reads from any other scitex package's
    state directory. Returned paths are sorted by agent name for stable
    ordering.
    """
    from pathlib import Path

    from ..config._resolve import _project_local_dirs

    # name -> yaml path; later writes are ignored (earlier = higher priority).
    found: dict[str, str] = {}

    home = Path.home()
    primary = home / ".scitex" / "agent-container" / "agents"
    # Project-local first (so an in-repo test agent wins over a stale
    # global with the same name), then home root, then env-port.
    search_dirs: list[Path] = list(_project_local_dirs())
    search_dirs.append(primary)

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
@click.argument(
    "targets", type=str, nargs=-1, required=True, shell_complete=agent_name_complete
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
@click.option(
    "--foreground",
    "foreground",
    is_flag=True,
    default=False,
    help=(
        "Run the agent attached to this terminal (no detach) and stream "
        "assistant output to stdout. Only meaningful for the "
        "claude-session runtime; ignored elsewhere. Single-target only — "
        "passing --foreground with multiple targets or a directory is an "
        "error."
    ),
)
@click.option(
    "--params-file",
    "params_file",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help=(
        "CSV with one row per agent instance. Header names ${VAR} "
        "placeholders to substitute in the template yaml; the 'name' "
        "column is required and supplies the per-instance dirname. "
        "Single-target only — TARGETS must be exactly one yaml. "
        "Materialised yamls land under ./params-fleet-out/<name>/<name>.yaml "
        "(override with --params-out)."
    ),
)
@click.option(
    "--params-out",
    "params_out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir for materialised yamls (default: ./params-fleet-out).",
)
@click.option(
    "--params-overwrite",
    "params_overwrite",
    is_flag=True,
    default=False,
    help="Replace existing materialised yamls under --params-out.",
)
def start(
    targets: tuple[str, ...],
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
    dry_run: bool,
    as_json: bool,
    yes: bool,
    foreground: bool,
    params_file: Path | None,
    params_out: Path | None,
    params_overwrite: bool,
) -> None:
    """Start one or more agents from YAML definitions.

    Each TARGET is either a YAML path, an agent name (resolved against the
    standard search paths), or a directory containing ``<name>/<name>.yaml``
    agent layouts. Multiple targets may be given.

    \b
    Example:
      $ sac agent start foo
      $ sac agent start ~/.scitex/agent-container/agents/foo/foo.yaml
      $ sac agent start foo bar baz
      $ sac agent start ~/.scitex/agent-container/agents/   # whole dir = bulk
    """
    import json as _json

    def _emit_json(payload: dict) -> None:
        click.echo(_json.dumps(payload, ensure_ascii=False))

    # F-CS2: --params-file expands a single template + CSV into N
    # materialised yamls. The materialised paths replace ``targets``
    # for the rest of the function so every downstream code path
    # (preflight, singleton check, runtime dispatch, JSON report)
    # treats them identically to ordinary multi-target invocations.
    if params_file is not None:
        if len(targets) != 1:
            click.echo(
                "Error: --params-file requires exactly one TARGET (the "
                "template yaml). Got "
                f"{len(targets)} targets.",
                err=True,
            )
            sys.exit(2)
        template_path = Path(targets[0]).expanduser()
        if not template_path.is_file():
            click.echo(
                f"Error: --params-file template not found: {template_path}",
                err=True,
            )
            sys.exit(2)
        out_dir = (params_out or Path("params-fleet-out")).expanduser()
        from .._state.fleet_template import expand_params_file

        try:
            materialised = expand_params_file(
                template_path,
                params_file,
                out_dir,
                overwrite=params_overwrite,
            )
        except (ValueError, FileExistsError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        targets = tuple(str(p) for p in materialised)
        if not as_json:
            console.print(
                f"[bold]--params-file[/bold]  expanded "
                f"{len(materialised)} agent(s) under [cyan]{out_dir}[/cyan]"
            )

    # Classify targets: directory targets expand to all <name>/<name>.yaml
    # under them; non-directory targets are paths or agent names.
    single_targets: list[str] = []
    bulk_yamls_from_dirs: list[str] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            for _name, yp in _iter_agent_yamls(p):
                bulk_yamls_from_dirs.append(yp)
        else:
            single_targets.append(t)
    is_bulk = bool(bulk_yamls_from_dirs)

    if (resume_id or session_mode) and is_bulk:
        click.echo(
            "Error: --resume / --session cannot be combined with directory "
            "targets (they would apply the same value to every agent).",
            err=True,
        )
        sys.exit(2)
    if foreground and (is_bulk or len(single_targets) > 1):
        click.echo(
            "Error: --foreground only works with a single agent target — "
            "the runner takes over the terminal until it exits.",
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

    # Bulk path: directory targets.
    if bulk_yamls_from_dirs:
        yamls = bulk_yamls_from_dirs
        if not yamls:
            console.print(
                "[dim]No agents found in "
                "~/.scitex/agent-container/agents/ or $SCITEX_AGENT_CONTAINER_YAML_DIRS[/dim]"
            )
            if not single_targets:
                return
        else:
            if not yes:
                click.echo(
                    f"Refusing to start {len(yamls)} agents without --yes/-y.",
                    err=True,
                )
                raise SystemExit(2)
            if True:
                try:
                    current_host = resolve_hostname()
                except RuntimeError:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
                    current_host = ""
                console.print(f"[blue]Starting {len(yamls)} agents...[/blue]")
                for yaml_path in yamls:
                    # stx-allow: fallback (reason: one agent's config parse or launch failure must not abort the remaining agents in a bulk start; printing FAILED and continuing is the correct bulk-safe behavior)
                    try:
                        config = load_config(yaml_path)
                        skip = _singleton_skip_reason(config, current_host)
                        if skip:
                            console.print(
                                f"  [yellow]SKIP[/yellow] {config.name}: {skip}"
                            )
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
                            "[green]DRY-RUN OK[/green]"
                            if dry_run
                            else "[green]OK[/green]"
                        )
                    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                        console.print(f"[red]FAILED: {exc}[/red]")
        if not single_targets:
            return

    # Per-target single-start loop.
    any_error = False
    for target_idx, raw_target in enumerate(single_targets):
        if target_idx > 0 and not as_json:
            console.print()  # blank line between agents

        # stx-allow: fallback (reason: config resolution, YAML parse, or agent_start can raise on misconfiguration or launch failure; catching here gives a clean error message and continues to the next target)
        try:
            config_path = resolve_with_prefix(raw_target)
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
                continue
            location = (
                f"REMOTE: {config.remote.host}" if config.remote.is_remote else "LOCAL"
            )
            if not as_json:
                console.print(
                    f"[blue]{'Dry-running' if dry_run else 'Starting'} agent "
                    f"'{config.name}' (runtime: {config.runtime}, {location})...[/blue]"
                )
                if no_preflight:
                    console.print(
                        "[dim]Preflight checks skipped (--no-preflight)[/dim]"
                    )
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
                foreground=foreground,
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
            any_error = True
            if as_json:
                _emit_json(
                    {
                        "name": raw_target,
                        "status": "error",
                        "error": str(exc),
                        "dry_run": dry_run,
                    }
                )
            else:
                console.print(f"[red]Error ({raw_target}): {exc}[/red]")
                traceback.print_exc()
    if any_error:
        sys.exit(1)


@click.command()
@click.argument(
    "targets", type=str, nargs=-1, required=True, shell_complete=agent_name_complete
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
    help="Skip confirmation prompt for bulk stop.",
)
def stop(
    targets: tuple[str, ...],
    force: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Stop one or more running agents.

    Each TARGET is an agent name, a YAML path, or a directory containing
    ``<name>/<name>.yaml`` agent layouts. Multiple targets may be given.

    \b
    Example:
      $ sac agent stop foo
      $ sac agent stop foo bar baz
      $ sac agent stop ~/.scitex/agent-container/agents/   # whole dir = bulk
      $ sac agent stop foo --dry-run
    """
    # Classify targets: directory targets expand to all <name>/<name>.yaml
    # under them; non-directory targets are agent names or YAML paths.
    single_targets: list[str] = []
    bulk_yamls_from_dirs: list[str] = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_dir():
            for _name, yp in _iter_agent_yamls(p):
                bulk_yamls_from_dirs.append(yp)
        else:
            single_targets.append(t)

    if dry_run:
        for t in single_targets:
            click.echo(f"[dry-run] would stop agent '{t}'")
        for yp in bulk_yamls_from_dirs:
            click.echo(f"[dry-run] would stop agent at '{yp}'")
        return

    # Refuse bulk stop without --yes/-y when directory targets resolved to ≥2 yamls.
    if len(bulk_yamls_from_dirs) > 1 and not yes:
        click.echo(
            f"Refusing to stop {len(bulk_yamls_from_dirs)} agents without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)

    # Bulk-from-dir-targets path
    any_error = False
    for yaml_path in bulk_yamls_from_dirs:
        try:
            config = load_config(yaml_path)
            agent_stop(config.name, force=force)
            console.print(f"[green]Agent '{config.name}' stopped[/green]")
        except Exception as exc:  # stx-allow: fallback (reason: one stop failure must not abort the remaining bulk stops)
            any_error = True
            console.print(f"[red]Error ({yaml_path}): {exc}[/red]")

    # Per-target single-stop loop
    for raw_target in single_targets:
        # stx-allow: fallback (reason: config resolution or agent_stop can raise if the agent is not in the registry or the session is already gone)
        try:
            name: str = raw_target
            if "/" in name or name.endswith((".yaml", ".yml")):
                config_path = resolve_with_prefix(name)
                config = load_config(config_path)
                name = config.name
            agent_stop(name, force=force)
            console.print(f"[green]Agent '{name}' stopped[/green]")
        except Exception as exc:
            any_error = True
            console.print(f"[red]Error ({raw_target}): {exc}[/red]")

    if any_error:
        sys.exit(1)


@click.command()
@click.argument("name", shell_complete=agent_name_complete)
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
      $ sac agent restart foo
      $ sac agent restart foo --dry-run
    """
    if dry_run:
        click.echo(f"[dry-run] would restart agent '{name}'")
        return
    if not yes:
        click.echo(f"Refusing to restart agent '{name}' without --yes/-y.", err=True)
        raise SystemExit(2)
    # stx-allow: fallback (reason: config resolution or agent_restart can raise if the agent is not running or the session cannot be found; error message + sys.exit(1) is cleaner than an unhandled traceback)
    try:
        if "/" in name or name.endswith((".yaml", ".yml")):
            config_path = resolve_with_prefix(name)
            config = load_config(config_path)
            name = config.name
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command(name="clean-registry")
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
      $ sac registry clean
      $ sac registry clean --dry-run
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
    if not yes:
        click.echo(
            "Refusing to remove stale registry entries without --yes/-y.", err=True
        )
        raise SystemExit(2)
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")
