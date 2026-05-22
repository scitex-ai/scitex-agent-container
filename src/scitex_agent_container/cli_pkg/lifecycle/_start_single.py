#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-target single-start loop for ``sac agents start``.

Extracted from ``_start.py`` (which would otherwise exceed the 512-line
per-file cap) — the click entry point stays a thin orchestrator and
delegates the per-target loop here. Mirrors the existing sibling
``_start_bulk.py::run_bulk_path``.
"""

from __future__ import annotations

import json as _json
import sys
import traceback
from typing import Callable

import click

from ..._lifecycle.lifecycle import agent_start
from ...config import load_config
from ...config._host import resolve_hostname
from ...config._resolve import resolve_with_prefix
from .._helpers import console, system_msg
from ._common import _multiplex_foreground_tails, _singleton_skip_reason
from ._dispatch import try_dispatch


def run_single_targets(
    single_targets: list[str],
    *,
    no_preflight: bool,
    force: bool,
    resume_id: str | None,
    session_mode: str | None,
    dry_run: bool,
    as_json: bool,
    foreground: bool,
    one_shot: bool,
    strict_drift: bool,
    no_redispatch: bool,
    multi_foreground: bool,
    preflight_runner: Callable[[], None],
) -> None:
    """Start each name/path in ``single_targets`` (directory bulk handled upstream).

    Exits non-zero (``sys.exit(1)``) when any target fails. Honours the
    cross-host dispatch branch, the singleton-skip guard, the JSON
    report shape, and the launch-time ``strict_drift`` escalation —
    behaviour is byte-identical to the inline loop it replaced.
    """

    def _emit_json(payload: dict) -> None:
        click.echo(_json.dumps(payload, ensure_ascii=False))


    if single_targets:
        preflight_runner()
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
            # Cross-host dispatch branch (routing only). Skipped when
            # --no-redispatch is passed (peer-side invocation uses this
            # to prevent recursion).
            if not no_redispatch:
                from ..._state.host_config import load as _load_host_config

                peers = _load_host_config().peers
                if try_dispatch(
                    config,
                    current_host,
                    peers,
                    dry_run=dry_run,
                    force=force,
                ):
                    continue
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
            # Location reads as `host@<host-workdir>:<container-workdir>`.
            host = resolve_hostname() or "local"
            host_workdir = config.expanded_workdir
            container_workdir = config.apptainer.container_workdir
            location = f"{host}@{host_workdir}:{container_workdir}"
            # `--foreground --json` redirect: keep the JSON summary off
            # the runner's trailing-newline-less stdout line.
            json_stream_err = as_json and foreground and not dry_run

            def _emit(obj, _err=json_stream_err):
                line = _json.dumps(obj, ensure_ascii=False)
                if _err:
                    line = "\n" + line
                click.echo(line, err=_err)

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
                strict_drift=strict_drift,
            )
            if as_json:
                from ..._state.port_allocator import get_port as _get_port
                from ..._state.state_db import now_iso as _now_iso

                _raw_port = getattr(getattr(config, "a2a", None), "port", None)
                _resolved_port: int | None = (
                    None if (dry_run or _raw_port is None) else _get_port(config.name)
                )
                _emit(
                    {
                        "name": config.name,
                        "status": "dry_run_ok" if dry_run else "started",
                        "host": host,
                        "host_workdir": host_workdir,
                        "container_workdir": container_workdir,
                        "dry_run": dry_run,
                        "a2a_port": _resolved_port,
                        "started_at": None if dry_run else _now_iso(),
                    }
                )
            else:
                if foreground and not dry_run:
                    # Agent stdout often lacks a trailing newline.
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
                        f"[yellow]auto_accept: false — manual TUI acceptance required on {host}[/yellow]"
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


__all__ = ["run_single_targets"]
