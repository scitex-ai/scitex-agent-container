"""Operator-facing ``--resume <uuid>`` preflight (#192, Part B #3).

When the operator EXPLICITLY asks ``sac agents start <name> --resume
<uuid>`` and that uuid does not exist in the agent's Claude Code projects
store, the recovery must be INFORMATIVE and SELECTABLE — never a silent
fresh start (operator design 4488):

  * fail loud BEFORE launching the runner,
  * list the conversations that ARE resumable for this agent (with
    timestamps + first-message snippets), and
  * point the operator at ``--resume <chosen>`` (an informed choice) or
    the EXPLICIT last-resort fresh start (``--session new-session``).

This is the synchronous, human-in-the-loop half of Part B #3. The
autonomous runner (which cannot prompt) keeps the documented last-resort
fresh start, but now surfaces the same candidate list loudly via
``session.jsonl`` (see ``_runners/_session_conversation.py``).

The conversations live inside the agent's container ``$HOME``: for a
local apptainer agent that is ``runtime/<name>/home/.claude/projects/
<encode(container_workdir)>/``. This module resolves that store and lists
the candidates. When the store can't be located (e.g. a remote agent
whose conversations live on another host), the preflight degrades to a
LOUD warning rather than a hard block — the runner-side candidate
surfacing then becomes the operator's window.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...config import AgentConfig

__all__ = ["ResumePreflightError", "preflight_resume_id"]


class ResumePreflightError(click.ClickException):
    """Raised when an explicit ``--resume <uuid>`` names an unknown session.

    A ``click.ClickException`` so the CLI prints the (informative) message
    and exits non-zero without a traceback — the operator sees the
    candidate list and the next-step hint, then re-runs with a chosen id.
    """


def _container_home(config: AgentConfig) -> Path | None:
    """Return the host-side path that backs the container ``$HOME``.

    For a local apptainer agent sac binds ``runtime/<name>/home/`` →
    ``/home/agent`` (ADR-0003 D6), so the conversations land under
    ``runtime/<name>/home/.claude/projects/``. Returns None when the
    runtime root / name can't be resolved (the caller then degrades to a
    loud warning rather than a hard block).
    """
    import os

    root = Path(
        os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
            str(Path.home() / ".scitex" / "agent-container" / "runtime"),
        )
    )
    home = root / config.name / "home"
    return home if home.is_dir() else None


def preflight_resume_id(
    config: AgentConfig,
    resume_id: str,
    *,
    is_remote: bool = False,
) -> None:
    """Validate an explicit ``--resume`` id; fail loud + informative on miss.

    Looks up the agent's resumable conversations in its container projects
    store. If ``resume_id`` matches one, returns silently (the resume is a
    valid choice). If it does not match:

      * with discoverable candidates → raise :class:`ResumePreflightError`
        listing them + the ``--resume <chosen>`` / explicit-fresh-start
        next steps;
      * with NO candidates → raise naming that nothing is resumable
        (start fresh with ``--session new-session``).

    When the store can't be located (remote agent, or the container home
    hasn't been materialised yet) the check degrades to a LOUD stderr
    warning and returns — it does not hard-block, because the id may be
    valid on the remote side and the runner will surface candidates via
    ``sac agents tail``.

    ``is_remote`` lets the caller skip the local-store assertion for
    cross-host agents (their conversations are not on this host).
    """
    from ..._runners._session_candidates import (
        format_candidates,
        list_session_candidates,
    )

    if is_remote:
        click.echo(
            f"[--resume] agent {config.name!r} is remote; the resume id "
            f"{resume_id!r} cannot be verified locally. If it is stale the "
            f"runner will surface the resumable conversations via "
            f"`sac agents tail {config.name}`.",
            err=True,
        )
        return

    home = _container_home(config)
    if home is None:
        click.echo(
            f"[--resume] could not locate the conversation store for "
            f"{config.name!r} (container home not materialised yet); the "
            f"resume id {resume_id!r} cannot be verified before launch. "
            f"If it is stale the runner will surface candidates via "
            f"`sac agents tail {config.name}`.",
            err=True,
        )
        return

    workdir = config.apptainer.container_workdir
    candidates = list_session_candidates(workdir, home=home)
    if any(c.session_id == resume_id for c in candidates):
        return  # valid choice — proceed to launch.

    listing = format_candidates(candidates)
    if candidates:
        raise ResumePreflightError(
            f"agent {config.name!r}: requested --resume {resume_id} but no "
            f"conversation with that id exists in the projects store.\n"
            f"Resumable conversations:\n{listing}\n"
            f"Re-run with `--resume <one-of-the-above>` to resume a specific "
            f"one, or `--session new-session` to start fresh explicitly."
        )
    raise ResumePreflightError(
        f"agent {config.name!r}: requested --resume {resume_id} but the "
        f"projects store holds no resumable conversations at all.\n{listing}\n"
        f"Start fresh explicitly with `--session new-session` (drop "
        f"--resume)."
    )
