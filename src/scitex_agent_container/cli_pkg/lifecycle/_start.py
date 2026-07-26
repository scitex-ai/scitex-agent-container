#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents start`` — launch one or more agents from YAML.

Split out of the former ``cli_pkg/lifecycle_cmds.py``. The command
covers single-target launches, directory-bulk launches, ``--params-file``
template fan-out, and the multi-target foreground multiplexer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .._helpers import agent_name_complete, console
from ._common import _iter_agent_yamls
from ._start_group_filter import apply_group_targets, group_option
from ._start_preflight_gate import make_preflight_runner


@click.command()
@click.argument(
    "targets",
    type=str,
    nargs=-1,
    required=False,  # --group NAME alone is valid (apply_group_targets below)
    shell_complete=agent_name_complete,
)
@group_option
@click.option(
    "--profile",
    metavar="NAME",
    help="Select a named launch profile (defaults to spec.default_profile).",
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
    "-n", "--tail-lines", "tail_lines", type=int, default=None,
    help="Trailing transcript messages to preview per resumable session "
    "on a stale --resume (sac-session-candidates-tail-preview).",
)
@click.option(
    "--session",
    "session_mode",
    type=click.Choice(
        # ``fresh`` is the canonical "always start a new session" value
        # (the default since 2026-06-22). ``new-session`` is kept as a
        # back-compat alias. Legacy ``continue-or-new`` / ``new`` are still
        # accepted at YAML load time via parse_claude but hidden from the
        # CLI surface.
        ["fresh", "continue", "new-session", "resume"],
        case_sensitive=False,
    ),
    default=None,
    help="Override the YAML's claude.session for this start invocation "
    "(fresh|continue|resume). Shorthand: --continue / --fresh.",
)
@click.option(
    "-c",
    "--continue",
    "continue_session",
    is_flag=True,
    default=False,
    help="Resume the agent's latest session (shorthand for --session "
    "continue). Overrides a spec that says fresh. For long-lived "
    "coordinators; experiment trials should stay fresh (the default).",
)
@click.option(
    "--fresh",
    "fresh_session",
    is_flag=True,
    default=False,
    help="Force a brand-new, independent session (shorthand for --session "
    "fresh). Overrides a spec that says continue. This is the default when "
    "no session flag is given.",
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
    "-v",
    "--verbose",
    "verbose",
    is_flag=True,
    default=False,
    help=(
        "Show the FULL effective launch-plan detail (mounts, env, skills, "
        "hooks, instruction sections, settings sources, host deep-merge — "
        "same as `sac agents explain`) in the refuse-without-`--yes` "
        "preview, instead of the default short summary (identity, spec "
        "path, runtime/image, workdir, model)."
    ),
)
@click.option(
    "--foreground",
    "foreground",
    is_flag=True,
    default=False,
    help="Run the agent attached to this terminal (no detach) and stream "
    "assistant output to stdout. Only meaningful for the "
    "claude-session runtime; ignored elsewhere. Single-target only — "
    "passing --foreground with multiple targets or a directory is an "
    "error.",
)
@click.option(
    "--one-shot",
    "one_shot",
    is_flag=True,
    default=False,
    help="Run the agent for ONE SDK turn (its startup_prompts), stream the "
    "reply, then exit. Requires spec.startup_prompts to be non-empty. "
    "Without this flag, the runner stays attached after the first "
    "turn so subsequent ``sac agents send`` calls reach the same "
    "session.",
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
@click.option(
    "--strict-drift",
    "strict_drift",
    is_flag=True,
    default=False,
    help="Hard-block (non-zero exit) on a drifted spec-source git repo "
    "instead of warn-and-launch. Equivalent to SAC_STRICT_DRIFT=1.",
)
@click.option(
    "--no-redispatch",
    "no_redispatch",
    is_flag=True,
    default=False,
    hidden=True,
    help=(
        "Skip the remote-dispatch branch (used by the peer-side invocation "
        "to prevent recursion). Internal use mostly."
    ),
)
@click.option(
    "--broker-self",
    "broker_self",
    is_flag=True,
    default=False,
    help=(
        "Bootstrap a per-invocation `sac listen` on a free loopback port "
        "and broker the spawn through it (nested SAC-from-SAC, L2 design). "
        "Use inside a SLURM allocation / parent SIF where no upstream "
        "`sac listen` is reachable. The listen is torn down on exit; the "
        "bearer never touches the operator's main token file."
    ),
)
@click.option(
    "--concurrency",
    "concurrency",
    type=int,
    default=3,
    show_default=True,
    help=(
        "Max agents to launch at once when MULTIPLE targets (or a bulk "
        "directory) are given. Each target is dispatched as its own "
        "`sac agents start` subprocess (process isolation — race-safe "
        "against the shared state DB / port allocator). Ignored for a "
        "single target (which keeps the unchanged in-process path)."
    ),
)
@click.option(
    "--stagger",
    "stagger",
    type=float,
    default=5.0,
    show_default=True,
    help=(
        "Seconds to wait between launching successive agents in a "
        "multi-target / bulk start, so N agents don't all hit the port "
        "allocator simultaneously. Ignored for a single target."
    ),
)
def start(
    targets: tuple[str, ...],
    groups: tuple[str, ...],
    profile: str | None,
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
    continue_session: bool,
    fresh_session: bool,
    dry_run: bool,
    as_json: bool,
    yes: bool,
    verbose: bool,
    foreground: bool,
    one_shot: bool,
    params_file: Path | None,
    params_out: Path | None,
    params_overwrite: bool,
    strict_drift: bool,
    no_redispatch: bool,
    broker_self: bool,
    concurrency: int,
    stagger: float,
    tail_lines: int | None,
) -> None:
    """Start one or more agents.

    Each TARGET may be an EXISTING agent — an agent name, a ``spec.yaml`` path,
    or a directory of ``<name>/spec.yaml`` agents (bulk) — OR a COLD-START form
    that materializes a minimal standardized TUI spec for an arbitrary project
    workdir and starts it (operator TODO 2026-06-17):

    \b
      <label>@<host>:/path/to/workdir   explicit label, host, and workdir
      <host>:/path/to/workdir           label = the workdir's basename
      /path/to/workdir                  host = this (the caller's) host
      .                                 workdir = the current directory

    Cold-start writes ``~/.scitex/agent-container/agents/<label>/{spec.yaml,
    to_home/}`` then launches it; ``@<host>`` dispatches to that host. Re-running
    a cold-start form reuses the spec when workdir+host match, else fails loud
    (use ``--force`` to overwrite). ``--dry-run`` prints the plan without writing
    or starting; ``--json`` emits it as structured output. Malformed forms fail
    loud (no silent fallback).

    An interactive launch (real tty, no ``--yes``) previews the effective plan
    then refuses to start — a short summary by default, or the FULL detail
    (mounts, env, hooks, ...) with ``-v``/``--verbose``.

    \b
    Example:
      $ sac start proj-figrecipe                       # existing agent (by name)
      $ sac start ~/.scitex/agent-container/agents/     # bulk dir = all agents
      $ sac start /home/me/proj/figrecipe               # cold-start, local host
      $ sac start fig@spartan:/home/me/proj/figrecipe   # cold-start on spartan
      $ sac start .                                     # cold-start the cwd
      $ sac start . --dry-run --json                    # preview the plan only
    """
    import json as _json

    def _emit_json(payload: dict) -> None:
        click.echo(_json.dumps(payload, ensure_ascii=False))

    targets = apply_group_targets(targets, groups)  # --group -> TARGETS merge
    # Session-continuity shorthand flags (--continue/-c, --fresh) fold into
    # session_mode. Validation + precedence live in ``_start_params`` to
    # keep this click entry under the per-file line cap.
    from ._start_params import resolve_session_shorthand

    session_mode = resolve_session_shorthand(
        continue_session=continue_session,
        fresh_session=fresh_session,
        session_mode=session_mode,
    )

    # F-CS2: --params-file expands a template + CSV into N materialised
    # yamls; the resulting paths replace ``targets`` so downstream code
    # (preflight, singleton check, JSON report) treats them identically.
    # Body lives in ``_start_params`` to keep this click entry under the
    # per-file line cap.
    if params_file is not None:
        from ._start_params import expand_params_targets

        targets = expand_params_targets(
            targets,
            params_file=params_file,
            params_out=params_out,
            params_overwrite=params_overwrite,
            as_json=as_json,
        )

    # Cold-start forms (operator TODO 2026-06-17): a target shaped like
    # <label>@<host>:/path, <host>:/path, an absolute /workdir, or "." is NOT an
    # existing agent — materialize a minimal standardized TUI spec for it, then
    # start it through the normal flow below. Plain agent names + explicit
    # spec.yaml paths + agent/agents-root dirs pass through untouched
    # (fs-precedence in resolve_cold_start_targets). Malformed forms fail loud.
    from ._cold_start import (
        ColdStartConflictError,
        ColdStartParseError,
        render_cold_start_plans,
        resolve_cold_start_targets,
    )

    try:
        from ...config._host import resolve_hostname

        _caller_host = resolve_hostname()
    except Exception:  # stx-allow: fallback (hostname resolution may fail in odd net envs; basename of socket name is the safe default)
        import socket

        _caller_host = socket.gethostname().split(".")[0]

    try:
        _rewritten, _cs_plans = resolve_cold_start_targets(
            targets,
            caller_host=_caller_host,
            dry_run=dry_run,
            force=force,
            # Use the SAME bulk-dir detector the classifier below uses, so an
            # agents-root dir (``<name>/<name>.yaml`` layout) is treated as an
            # existing bulk target, not cold-started.
            dir_has_agents=lambda p: bool(_iter_agent_yamls(p)),
        )
    except (ColdStartParseError, ColdStartConflictError) as exc:
        if as_json:
            _emit_json({"error": str(exc)})
        else:
            click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    render_cold_start_plans(
        _cs_plans, as_json=as_json, emit_json=_emit_json, console=console
    )
    targets = tuple(_rewritten)
    if not targets:
        # All targets were dry-run cold-starts: plan(s) shown, nothing to launch.
        if not as_json:
            console.print("[dim]--dry-run: spec(s) planned above; not started.[/dim]")
        return

    # Classify targets: directory targets expand to all <name>/<name>.yaml
    # under them; non-directory targets are paths or agent names. Body
    # lives in ``_start_params`` to keep this click entry under the cap.
    from ._start_params import classify_targets

    single_targets, bulk_yamls_from_dirs = classify_targets(
        targets, iter_agent_yamls=_iter_agent_yamls
    )
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

    # Preflight OAuth credential expiry — idempotent, lazy / once per
    # invocation, shared by the single / bulk / parallel dispatch paths.
    # The factory lives in ``_start_preflight_gate`` (which owns the
    # skip rules: --no-redispatch, --broker-self, all-provider-backed).
    _run_preflight_once = make_preflight_runner(
        single_targets=single_targets,
        bulk_yamls=bulk_yamls_from_dirs,
        no_redispatch=no_redispatch,
        broker_self=broker_self,
        profile=profile,
    )

    # Serialized multi-start queue (sac-multi-start-queue-oauth, Half-A):
    # when MULTIPLE targets (or a bulk directory) are launched in one
    # invocation, dispatch each as its OWN ``sac agents start <target>
    # --yes --no-redispatch`` subprocess, bounded by ``--concurrency``
    # and spaced ``--stagger`` seconds apart. Process isolation makes
    # this race-safe against the shared state DB / port allocator. A
    # SINGLE target keeps the unchanged in-process path below. The guard
    # + dispatch live in ``_start_parallel`` to keep this click entry
    # under the per-file line cap; it returns True iff it handled the
    # launch (multi-target + no per-target-interactive / report flag).
    from ._start_parallel import maybe_run_parallel

    if maybe_run_parallel(
        single_targets=single_targets,
        bulk_yamls=bulk_yamls_from_dirs,
        concurrency=concurrency,
        stagger=stagger,
        yes=yes,
        no_preflight=no_preflight,
        force=force,
        session_mode=session_mode,
        strict_drift=strict_drift,
        broker_self=broker_self,
        foreground=foreground,
        multi_foreground=multi_foreground,
        one_shot=one_shot,
        resume_id=resume_id,
        dry_run=dry_run,
        as_json=as_json,
        preflight_runner=_run_preflight_once,
        profile=profile,
    ):
        return

    # Bulk path: directory targets. Body lives in ``_start_bulk`` to
    # keep the click entry under the per-file line cap.
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
            from ._start_bulk import run_bulk_path

            run_bulk_path(
                yamls,
                yes=yes,
                no_preflight=no_preflight,
                force=force,
                dry_run=dry_run,
                preflight_runner=_run_preflight_once,
                profile=profile,
            )
        if not single_targets:
            return

    # Per-target single-start loop. Body lives in ``_start_single`` to
    # keep this click entry under the per-file line cap.
    from ._start_single import run_single_targets

    run_single_targets(
        single_targets,
        no_preflight=no_preflight,
        force=force,
        resume_id=resume_id,
        session_mode=session_mode,
        dry_run=dry_run,
        as_json=as_json,
        foreground=foreground,
        one_shot=one_shot,
        strict_drift=strict_drift,
        no_redispatch=no_redispatch,
        multi_foreground=multi_foreground,
        preflight_runner=_run_preflight_once,
        broker_self=broker_self,
        yes=yes,
        verbose=verbose,
        tail_lines=tail_lines,
        profile=profile,
    )


__all__ = ["start"]
