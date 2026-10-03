"""Explicit config/runtime ownership for lifecycle session preservation."""

from pathlib import Path

from .._runners._hermes_owned_session import (
    SessionEvidenceError,
    has_owned_session_candidate,
    read_runtime_owned_hermes_session_id,
)
from .._runners._session_id import read_session_id


def read_session_id_for(config, runtime, state_dir: Path, *, active_sessions_fn=None):
    """Keep SDK precedence; Hermes aliases require the actual runtime owner."""
    try:
        (state_dir / "session_id").lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SessionEvidenceError(
            "Session evidence unavailable: SDK marker cannot be inspected"
        ) from error
    else:
        return read_session_id(state_dir)
    if not has_owned_session_candidate(state_dir):
        return read_session_id(state_dir)
    from ..config._harness_registry import HERMES_TUI, resolve_harness_key

    try:
        selected = resolve_harness_key(config)
    except ValueError:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: selected-harness-unavailable"
        ) from None
    if selected != HERMES_TUI:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: selected-harness-is-not-hermes"
        )
    resolver = getattr(runtime, "_state_dir", None)
    if callable(resolver):
        owned_state = resolver(config)
    else:
        from ..runtimes.tui_session import TuiSessionRuntime, state_dir_for_config

        if not isinstance(runtime, TuiSessionRuntime):
            raise SessionEvidenceError(
                "Hermes session evidence is unproven: runtime-state-owner-unavailable"
            )
        owned_state = state_dir_for_config(config)
    try:
        if Path(owned_state).resolve() != state_dir.resolve():
            raise SessionEvidenceError(
                "Hermes session evidence is unproven: runtime-state-owner-mismatch"
            )
    except (OSError, TypeError) as error:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: runtime-state-owner-unavailable"
        ) from error
    if active_sessions_fn is None:
        from ..runtimes._hermes_tui_rpc import active_sessions

        active_sessions_fn = active_sessions
    return read_runtime_owned_hermes_session_id(
        state_dir, active_sessions_fn=active_sessions_fn
    )
