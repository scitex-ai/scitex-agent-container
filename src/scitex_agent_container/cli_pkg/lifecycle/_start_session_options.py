#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The ``sac agents start`` SESSION-CONTINUITY option group.

Extracted from ``_start.py`` under the project's 512-line per-file cap,
following the sibling ``_start_gate_options`` / ``_start_group_filter``
pattern: the click entry stays a thin orchestrator and each cohesive
option group owns its own module.

These five flags answer ONE question — which conversation does this
launch attach to? — so they belong together:

  ``--resume <id>``     attach to a named transcript uuid.
  ``-n/--tail-lines``   how much of each candidate transcript the stale
                        ``--resume`` preflight previews.
  ``--session <mode>``  fresh | continue | new-session | resume.
  ``-c/--continue``     shorthand for ``--session continue``.
  ``--fresh``           shorthand for ``--session fresh``.

Values are EXPOSED (unlike the spec-gate group's env-transported
overrides): they are threaded through ``start -> run_single_targets ->
agent_start`` as ``session_override`` / ``resume_id_override``, and
``_start_parallel`` re-appends them to each child argv itself.
"""

from __future__ import annotations

import click


def session_options(func):
    """Apply the five session-continuity flags to a click command, in help order."""
    options = (
        click.option(
            "--resume",
            "resume_id",
            type=str,
            default=None,
            help="Resume a specific Claude Code session by ID (e.g. the UUID of the "
            "*.jsonl under ~/.claude/projects/<encoded>/). Implies --session resume "
            "and overrides the YAML's claude.session / claude.resume_id.",
        ),
        click.option(
            "-n",
            "--tail-lines",
            "tail_lines",
            type=int,
            default=None,
            help="Trailing transcript messages to preview per resumable session "
            "on a stale --resume (sac-session-candidates-tail-preview).",
        ),
        click.option(
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
        ),
        click.option(
            "-c",
            "--continue",
            "continue_session",
            is_flag=True,
            default=False,
            help="Resume the agent's latest session (shorthand for --session "
            "continue). Overrides a spec that says fresh. For long-lived "
            "coordinators; experiment trials should stay fresh (the default).",
        ),
        click.option(
            "--fresh",
            "fresh_session",
            is_flag=True,
            default=False,
            help="Force a brand-new, independent session (shorthand for --session "
            "fresh). Overrides a spec that says continue. This is the default when "
            "no session flag is given.",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


__all__ = ["session_options"]
