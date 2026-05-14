#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents start`` — launch one or more agents from YAML.

Split out of the former ``cli_pkg/lifecycle_cmds.py``. The command
covers single-target launches, directory-bulk launches, ``--params-file``
template fan-out, and the multi-target foreground multiplexer.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import click

from ..._lifecycle.lifecycle import agent_start
from ...config import load_config
from ...config._host import resolve_hostname
from ...config._resolve import resolve_with_prefix
from .._helpers import agent_name_complete, console, system_msg
from ._common import (
    _iter_agent_yamls,
    _multiplex_foreground_tails,
    _singleton_skip_reason,
)


@click.command()
@click.argument(
    "targets",
    type=str,
    nargs=-1,
    required=True,
    shell_complete=agent_name_complete,
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
        # New names (REQUIREMENT_SUMMARY §3 #6); legacy aliases
        # `continue-or-new` and `new` are still accepted at YAML load
        # time via parse_claude but hidden from the CLI surface.
        ["continue", "new-session", "resume"],
        case_sensitive=False,
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
    "--one-shot",
    "one_shot",
    is_flag=True,
    default=False,
    help=(
        "Run the agent for ONE SDK turn (its startup_prompts), stream the "
        "reply, then exit. Requires spec.startup_prompts to be non-empty. "
        "Without this flag, the runner stays attached after the first "
        "turn so subsequent ``sac agents send`` calls reach the same "
        "session."
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
    one_shot: bool,
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
        from ..._state.fleet_template import expand_params_file

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
    # --foreground semantics:
    #   * single target: pass foreground=True down to the runtime so
    #     the SDK runner attaches its stdio to this terminal.
    #   * multi target: can't hand the tty to N runners; start all in
    #     background, then multiplex session.jsonl tails with a
    #     `[<name>]` line-prefix until every heartbeat reports
    #     "stopping" (or the operator hits Ctrl-C).
    multi_foreground = foreground and (is_bulk or len(single_targets) > 1)
    if multi_foreground:
        foreground = False  # disable per-runtime attach; we multiplex.
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
                console.print(f"=== [blue]Starting {len(yamls)} agents...[/blue] ===")
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
            # Location reads as `host@<host-workdir>:<container-workdir>`
            # so the operator sees:
            #   * which host the agent runs on
            #   * the host-side dir that gets bind-mounted into the
            #     container (= spec.workdir)
            #   * the path the agent sees inside the container
            # The container side is always /work — fixed by sac
            # (`--bind <workdir>:/work` in _apptainer_runtime).
            host = (
                config.remote.host
                if config.remote.is_remote
                else (resolve_hostname() or "local")
            )
            host_workdir = config.expanded_workdir
            container_workdir = config.apptainer.container_workdir
            location = f"{host}@{host_workdir}:{container_workdir}"
            # `--foreground --json` was emitting the JSON summary on the
            # same line as the runner's tail-of-stdout (Claude's reply
            # has no trailing newline). Redirect the JSON to stderr in
            # that combo + lead with a `\n` so interactive ttys (stderr
            # and stdout glued together) still get visual separation.
            json_stream_err = as_json and foreground and not dry_run

            def _emit(obj):
                line = _json.dumps(obj, ensure_ascii=False)
                if json_stream_err:
                    line = "\n" + line
                click.echo(line, err=json_stream_err)

            if not as_json:
                verb_now = "dry-run" if dry_run else "starting"
                system_msg(
                    f"[dim]{verb_now}[/dim] [bold]{config.name}[/bold] "
                    f"[dim]→ {location}[/dim]"
                )
                if no_preflight:
                    system_msg("preflight skipped (--no-preflight)", style="dim")
                if force:
                    system_msg(
                        "force mode — stopping any existing instance first",
                        style="dim",
                    )
                if session_mode:
                    msg = f"session override: claude.session = {session_mode}"
                    if resume_id:
                        msg += f", resume_id = {resume_id}"
                    system_msg(msg, style="dim")
            agent_start(
                config_path,
                no_preflight=no_preflight,
                force=force,
                dry_run=dry_run,
                session_override=session_mode,
                resume_id_override=resume_id,
                foreground=foreground,
                one_shot=one_shot,
            )
            if as_json:
                _emit(
                    {
                        "name": config.name,
                        "status": "dry_run_ok" if dry_run else "started",
                        "host": host,
                        "host_workdir": host_workdir,
                        "container_workdir": container_workdir,
                        "dry_run": dry_run,
                    }
                )
            else:
                if foreground and not dry_run:
                    # Agent stdout often lacks a trailing newline; break
                    # the join before our success summary lands.
                    click.echo("")
                verb = "dry-run prepared the workspace for" if dry_run else "started"
                tail = "" if dry_run else f" [dim]({location})[/dim]"
                system_msg(f"[bold]{config.name}[/bold] {verb}{tail}", style="green")
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

    if multi_foreground and not dry_run:
        _multiplex_foreground_tails(single_targets)


__all__ = ["start"]
