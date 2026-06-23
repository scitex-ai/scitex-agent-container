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


def _any_target_needs_anthropic_oauth(
    single_targets: list[str], bulk_yamls: list[str]
) -> bool:
    """Return True iff at least one target spec uses Anthropic OAuth.

    PR#314 (lead msg 24a8b27c): provider-backed specs (LiteLLM, vLLM,
    DeepSeek, gateway via ``ANTHROPIC_BASE_URL``) route the SDK session
    through a non-Anthropic backend; the parent's
    ``~/.claude/.credentials.json`` is bind-mounted but never read.
    When EVERY target has ``spec.claude.provider`` non-None, the
    parent's OAuth preflight is moot.

    Defensive default: if a spec fails to load (unresolvable name,
    unparseable YAML, schema error), assume it needs OAuth. Better to
    ask the operator for creds + surface the real spec error on the
    actual dispatch loop than to skip the gate silently on a broken
    spec the operator hasn't noticed yet.
    """
    from ...config import load_config
    from ...config._resolve import resolve_with_prefix

    for raw in list(single_targets) + list(bulk_yamls):
        try:
            cfg_path = resolve_with_prefix(raw)
            cfg = load_config(cfg_path)
        except Exception:  # stx-allow: fallback (reason: defensive — see docstring)
            return True
        provider = getattr(getattr(cfg, "claude", None), "provider", None)
        if provider is None:
            return True
    return False


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
def start(
    targets: tuple[str, ...],
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
    continue_session: bool,
    fresh_session: bool,
    dry_run: bool,
    as_json: bool,
    yes: bool,
    foreground: bool,
    one_shot: bool,
    params_file: Path | None,
    params_out: Path | None,
    params_overwrite: bool,
    strict_drift: bool,
    no_redispatch: bool,
    broker_self: bool,
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

    # Session-continuity shorthand flags (--continue/-c, --fresh) fold into
    # session_mode. They are mutually exclusive with each other and may not
    # contradict an explicit --session. ``--continue`` overrides a spec that
    # says fresh; ``--fresh`` overrides a spec that says continue — both via
    # the normal session_override path (claude.session is mutated post-load),
    # which is what the runtime/argv builder reads (precedence: CLI > spec >
    # role-default > global default fresh).
    if continue_session and fresh_session:
        click.echo("Error: --continue and --fresh are mutually exclusive.", err=True)
        sys.exit(2)
    _shorthand = (
        "continue" if continue_session else ("fresh" if fresh_session else None)
    )
    if _shorthand is not None:
        if session_mode is not None and session_mode.lower() != _shorthand:
            click.echo(
                f"Error: --{_shorthand} contradicts --session {session_mode}; "
                "pass only one.",
                err=True,
            )
            sys.exit(2)
        session_mode = _shorthand

    # F-CS2: --params-file expands a template + CSV into N materialised
    # yamls; the resulting paths replace ``targets`` so downstream code
    # (preflight, singleton check, JSON report) treats them identically.
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

    # Cold-start forms (operator TODO 2026-06-17): a target shaped like
    # <label>@<host>:/path, <host>:/path, an absolute /workdir, or "." is NOT an
    # existing agent — materialize a minimal standardized TUI spec for it, then
    # start it through the normal flow below. Plain agent names + explicit
    # spec.yaml paths + agent/agents-root dirs pass through untouched
    # (fs-precedence in resolve_cold_start_targets). Malformed forms fail loud.
    from ._cold_start import (
        ColdStartConflictError,
        ColdStartParseError,
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

    for _plan in _cs_plans:
        if as_json:
            _emit_json(
                {
                    "cold_start": {
                        "label": _plan.label,
                        "host": _plan.host,
                        "workdir": _plan.workdir,
                        "spec_path": _plan.spec_path,
                        "action": _plan.action,
                    }
                }
            )
        else:
            console.print(
                f"[bold]cold-start[/bold] [cyan]{_plan.label}[/cyan] "
                f"[dim]({_plan.action})[/dim]  host=[cyan]{_plan.host}[/cyan]  "
                f"workdir=[cyan]{_plan.workdir}[/cyan]\n"
                f"  spec: [dim]{_plan.spec_path}[/dim]"
            )
    targets = tuple(_rewritten)
    if not targets:
        # All targets were dry-run cold-starts: plan(s) shown, nothing to launch.
        if not as_json:
            console.print("[dim]--dry-run: spec(s) planned above; not started.[/dim]")
        return

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

    # Preflight OAuth credential expiry. Lazy / one-shot: runs only
    # when we're about to dispatch (skips pure-validation exits), once
    # per invocation, skipped on --no-redispatch and when an
    # ANTHROPIC_API_KEY / SAC_ANTHROPIC_API_KEY is set (api-key path —
    # see provision_anthropic_auth in runtimes/_sdk_common.py). On
    # failure: exit 1 with the helper's message on stderr, no
    # traceback.
    #
    # PR#314 (lead msg 24a8b27c / clew Spartan dogfood 2026-06-06):
    # also skip when the invocation is purely orchestrator-shaped:
    #   * ``--broker-self`` parent — never talks to Anthropic; only
    #     bootstraps a listen + spawns the capsule. The capsule's own
    #     preflight runs separately (in the broker's child subprocess).
    #   * EVERY target's spec.claude.provider is non-None — the SDK
    #     session routes through a non-Anthropic backend (LiteLLM /
    #     vLLM / DeepSeek / gateway via ANTHROPIC_BASE_URL), and the
    #     bind-mounted Anthropic credentials are never read.
    # Either condition is sufficient to skip; the gate stays surgical
    # — any Anthropic-backed target still triggers the check.
    _preflight_ran = False

    def _run_preflight_once() -> None:
        nonlocal _preflight_ran
        if _preflight_ran or no_redispatch:
            return
        _preflight_ran = True
        # Orchestrator-only invocation — skip the parent's OAuth check.
        if broker_self:
            return
        # All targets provider-backed — no parent-side Anthropic OAuth
        # needed. Loaded once per invocation (cheap; the same configs
        # are re-loaded inside run_single_targets / run_bulk_path for
        # actual dispatch — a future refactor could thread the loaded
        # configs through, but the duplication is small enough to leave
        # for now).
        if not _any_target_needs_anthropic_oauth(single_targets, bulk_yamls_from_dirs):
            return
        from ..._state._preflight_creds import check_oauth_token_expiry

        try:
            check_oauth_token_expiry()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

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
    )


__all__ = ["start"]
