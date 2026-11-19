"""Module-level support for the ``tui`` runtime (:mod:`.tui_session`).

Split out of ``tui_session.py`` (over the line limit) with no behaviour
change: the session-name helpers, the per-agent state dir, the pure
start-success decision and the runtime's tunables. ``tui_session``
re-exports every public name, so existing imports keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ._tui_session_names import (
    candidate_session_names,
    canonical_session_name,
    legacy_session_name,
    resolve_live_session,
)

__all__ = [
    "TuiStopVerificationError",
    "candidate_session_names_for",
    "legacy_session_name_for",
    "resolve_live_session_for",
    "session_name_for",
    "start_succeeded",
    "state_dir_for_config",
]


class TuiStopVerificationError(RuntimeError):
    """The owned tmux session or process cgroup survived teardown."""


_CLAUDE_BIN_DEFAULT = "claude"

# Default max-idle window for the RESPONSIVENESS probe
# (``TuiSessionRuntime.is_responsive``; liveness/``is_running`` no longer
# uses it). 300s mirrors the SDK ``health.interval`` default so a
# quiet-but-healthy TUI has grace. Overridable per-call via ``max_idle_s``.
_DEFAULT_MAX_IDLE_S = 300.0

# Boot-drain window when ``spec.startup_commands`` delay ``exec claude``
# (e.g. an in-container ``uv pip install`` that runs for minutes before
# the TUI launches). The drain polls through the bootstrap and dismisses
# claude's first-run modals (bypass-permissions / trust / theme) the
# moment they appear; it returns as soon as claude is up, so this is a
# CAP, not a fixed block. 240s comfortably covers a cold uv resolve.
_STARTUP_BOOT_DRAIN_S = 240.0


def start_succeeded(
    *,
    session_alive: bool,
    reached_ready: bool | None,
    is_running: bool,
) -> bool:
    """Pure decision: did the TUI start actually succeed? (BUG 3 — no false success.)

    ``tmux new-session`` succeeding is NOT success — the inner claude can die
    mid-boot (e.g. an Escape cancelled the dev-channels modal) or hang before
    binding its input. This function encodes the fail-loud contract so it is
    unit-testable without a live TUI:

      * ``session_alive`` False → FAILURE. A dead tmux session can never be a
        running agent, whatever the drain thought.
      * ``session_alive`` True and ``reached_ready`` True → SUCCESS. The
        boot-drain observed the input-ready / ``bypass permissions`` marker.
      * ``session_alive`` True and ``reached_ready`` False → FAILURE. The drain
        RAN and did NOT reach ready within its window (the modal never cleared /
        claude never bound input) — loud failure, not a claimed success.
      * ``session_alive`` True and ``reached_ready`` None (drain disabled this
        boot) → defer to ``is_running`` (the liveness probe): alive+responsive
        counts as success, otherwise failure.
    """
    if not session_alive:
        return False
    if reached_ready is True:
        return True
    if reached_ready is False:
        return False
    return is_running


def session_name_for(config: AgentConfig) -> str:
    """Return the tmux session name sac mints for this agent: the bare name.

    New sessions carry NO prefix — the session name IS the agent name.
    Sessions minted before the cutover are named ``tui-<name>`` and are still
    recognised: use :func:`candidate_session_names_for` / :func:`resolve_live_session_for`
    for any read path, and :func:`legacy_session_name_for` only to name the
    legacy form explicitly. ``TmuxManager.exists`` keys off the same string,
    so the runtime probes its own session deterministically.
    """
    return canonical_session_name(config.name)


def legacy_session_name_for(config: AgentConfig) -> str:
    """The pre-cutover ``tui-<name>`` session name. Recognised, never minted."""
    return legacy_session_name(config.name)


def candidate_session_names_for(config: AgentConfig) -> tuple[str, str]:
    """Every session name this agent could be running under, preferred first.

    Canonical bare first, legacy ``tui-`` second — the same order
    :func:`._tui_session_names.candidate_session_names` defines.
    """
    return candidate_session_names(config.name)


def resolve_live_session_for(
    config: AgentConfig, sessions: Any | None = None
) -> str:
    """The live session for this agent: bare when present, else legacy.

    ``sessions`` is a tmux session listing, or — the common runtime case — a
    ``TmuxManager``-shaped multiplexer exposing ``exists`` / ``list_sessions``.
    ``None`` resolves to the canonical bare name (the minting default).
    """
    if sessions is None:
        return canonical_session_name(config.name)
    if hasattr(sessions, "list_sessions"):
        try:
            return resolve_live_session(config.name, sessions.list_sessions())
        except Exception:  # stx-allow: fallback (an unreadable listing resolves to the canonical name — callers treat probe failure as unknown, never as absence)
            return canonical_session_name(config.name)
    if hasattr(sessions, "exists"):
        for candidate in candidate_session_names(config.name):
            try:
                if sessions.exists(candidate):
                    return candidate
            except Exception:  # stx-allow: fallback (a failed exists-probe is blindness on that candidate, not absence — keep trying the next one)
                continue
        return canonical_session_name(config.name)
    return resolve_live_session(config.name, sessions)


def state_dir_for_config(config: AgentConfig) -> Path:
    """Per-agent state dir on the host: project-local if the agent's spec lives
    under a project-scope ``.scitex/agent-container/`` tree, else
    ``~/.scitex/agent-container/runtime/<name>/``. Mirrors
    ``ClaudeSessionRuntime._state_dir`` so both runtimes land per-agent files in
    the same place.
    """
    from .._runners import claude_session as _runner

    src = getattr(config, "config_path", "") or ""
    root: Path | None = None
    if src:
        try:
            from scitex_config._ecosystem import local_state

            scope = local_state.find_project_scope(
                "agent-container", start=Path(src).parent
            )
            if scope is not None:
                root = scope / "runtime"
        except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state — the same fallback the SDK runtime uses, see runtimes/claude_session.py::_project_runtime_root)
            root = None
    return _runner.state_dir_for(config.name, root=root)
