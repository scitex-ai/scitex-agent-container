"""Context-bound command transitions for the Hermes TUI owner."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable


def requested_session(command: list[str]) -> tuple[str, str]:
    for index, value in enumerate(command[:-1]):
        if value in {"--continue", "-c"}:
            return "continue", str(command[index + 1]).strip()
        if value in {"--resume", "-r"}:
            return "resume", str(command[index + 1]).strip()
    return "fresh", ""


def command_value(command: list[str], flag: str) -> str:
    try:
        return str(command[command.index(flag) + 1]).strip()
    except (ValueError, IndexError):
        return ""


def resume_command(command: list[str], stored_session_id: str) -> list[str]:
    """Resume one exact transcript without replaying the startup task."""
    rebuilt: list[str] = []
    skip_value = False
    value_flags = {"--continue", "-c", "--resume", "-r", "--query"}
    for value in command:
        if skip_value:
            skip_value = False
            continue
        if value in value_flags:
            skip_value = True
            continue
        if value == "--create-if-missing":
            continue
        rebuilt.append(value)
    return rebuilt + ["--resume", stored_session_id]


def fresh_command(command: list[str], *, preserve_query: bool) -> list[str]:
    """Drop every continuation selector; optionally keep one new-task query."""
    rebuilt: list[str] = []
    skip_value = False
    value_flags = {"--continue", "-c", "--resume", "-r"}
    if not preserve_query:
        value_flags.update({"--query", "--query-file", "-q"})
    for value in command:
        if skip_value:
            skip_value = False
            continue
        if value in value_flags:
            skip_value = True
            continue
        if value == "--create-if-missing":
            continue
        rebuilt.append(value)
    return rebuilt


def reconcile_command(command: list[str]) -> list[str]:
    """Re-open the named session without replaying its startup turn."""
    rebuilt: list[str] = []
    skip_value = False
    for value in command:
        if skip_value:
            skip_value = False
            continue
        if value in {"--query", "--query-file", "-q"}:
            skip_value = True
            continue
        rebuilt.append(value)
    return rebuilt


def apply_session_age_policy(
    command: list[str],
    *,
    state_dir: Path,
    max_age_minutes: int,
    stored_lookup: Callable[[Path, str, str], dict | None],
    now: Callable[[], float] = time.time,
) -> list[str]:
    """Start fresh when a named continuation is at or beyond its age cap."""
    from ._hermes_context_gc import continuation_is_fresh_enough

    mode, identity = requested_session(command)
    if mode not in {"continue", "resume"} or not identity:
        return command
    try:
        record = stored_lookup(state_dir, identity, mode)
    except Exception:
        record = {"started_at": None}
    if record is None:
        return command
    if continuation_is_fresh_enough(
        record.get("started_at"), now=now(), max_age_minutes=max_age_minutes
    ):
        return command
    return fresh_command(command, preserve_query=True)


def transition_tui_child(
    *,
    tui: Any,
    command: list[str],
    replacement: str,
    env: dict[str, str],
    spawn: Callable[..., Any],
    terminate: Callable[[Any], None],
    on_spawn: Callable[[Any], None] | None,
) -> tuple[Any, list[str], str, str, str]:
    """Replace only the TUI child; return child/command/session identity state."""
    terminate(tui)
    if replacement:
        command = resume_command(command, replacement)
        session_mode, expected_identity, resume_key = "resume", replacement, replacement
    else:
        command = fresh_command(command, preserve_query=False)
        session_mode, expected_identity, resume_key = "fresh", "", ""
    child = spawn(command, env=env)
    if on_spawn is not None:
        on_spawn(child)
    return child, command, session_mode, expected_identity, resume_key


def make_session_observer(
    state_dir: Path, command: list[str]
) -> Callable[[dict], str | None]:
    """Build the owner callback for heartbeat projection and context lifecycle."""
    def observe(session: dict) -> str | None:
        try:
            from ._hermes_heartbeat_projection import (
                refresh_hermes_heartbeat_projection,
            )

            refresh_hermes_heartbeat_projection(state_dir, state_dir.name)
        except Exception:
            pass
        from ._hermes_context_gc import reconcile_context_lifecycle
        from ._hermes_context_rpc import reconcile_pending_transition

        workdir = Path(command_value(command, "--in") or os.getcwd())
        reconcile_pending_transition(state_dir)
        return reconcile_context_lifecycle(
            state_dir=state_dir,
            agent_name=state_dir.name,
            workdir=workdir,
            observed_session=session,
            engine_model=command_value(command, "--model"),
            engine_provider=command_value(command, "--provider"),
        )

    return observe


def clear_session_heartbeat(state_dir: Path, session: dict) -> None:
    """Remove the proven live session's persisted model-calling heartbeat."""
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        raise RuntimeError("owned Hermes session has no id")
    from ._hermes_tui_rpc import clear_heartbeat_for_session

    clear_heartbeat_for_session(state_dir, session_id)


__all__ = [
    "apply_session_age_policy",
    "clear_session_heartbeat",
    "command_value",
    "fresh_command",
    "make_session_observer",
    "reconcile_command",
    "requested_session",
    "resume_command",
    "transition_tui_child",
]
