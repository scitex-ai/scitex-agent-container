"""Stable Hermes session identity owned by SAC."""

from __future__ import annotations


def hermes_session_key(agent_name: str, engine_key: str) -> str:
    """Return the engine-scoped name used for automatic continuation."""
    agent = str(agent_name).strip()
    engine = str(engine_key).strip()
    if not agent or not engine:
        raise ValueError("Hermes session identity requires agent and engine keys")
    return f"sac:{agent}:{engine}"


__all__ = ["hermes_session_key"]
