"""Lossless peer-side argv for a cross-host lifecycle start."""

from __future__ import annotations


def spawned_by() -> str:
    """Launching identity recorded for a remotely dispatched start."""
    from ..._env import getenv

    return getenv("NAME") or "cli"


def remote_start_argv(
    name: str,
    *,
    engine: str | None = None,
    session_mode: str | None = None,
    resume_id: str | None = None,
) -> list[str]:
    """Carry start-time engine and conversation choices across SSH."""
    argv = ["sac", "agents", "start", name, "--no-redispatch", "--json"]
    if engine:
        argv += ["--engine", engine]
    if resume_id:
        argv += ["--resume", resume_id]
    elif session_mode:
        argv += ["--session", session_mode]
    return argv


__all__ = ["remote_start_argv", "spawned_by"]
