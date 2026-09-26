"""Resolve listener status specs from the active registry row first."""

from __future__ import annotations

from pathlib import Path

from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ..config._resolve import resolve_config


class InvalidRegistryConfig(ValueError):
    """The active registry row does not name a usable config path."""


class StatusSpecUnreadable(RuntimeError):
    """The selected status spec exists as authority but cannot be read."""


def resolve_status_spec(name: str, *, registry: Registry | None = None) -> str:
    """Return the spec owned by the current registry row for ``name``.

    An active registry row is an incarnation record: its exact ``config`` is
    authoritative even when another same-name spec exists in a configured
    search tree.  Name-based resolution remains the fallback for unregistered
    specs and self-peers, preserving the route's historical discovery surface.
    """
    row = (registry or Registry()).get(name)
    if row is None:
        return resolve_config(name)

    config = row.get("config")
    if not isinstance(config, str) or not config.strip():
        raise InvalidRegistryConfig(
            f"Active registry row for agent {name!r} has no usable config path"
        )
    return str(Path(config).expanduser())


def load_status_config(name: str) -> tuple[str, AgentConfig]:
    """Select one authoritative status spec and load that exact path."""
    path = resolve_status_spec(name)
    try:
        return path, load_config(path)
    except OSError as exc:
        raise StatusSpecUnreadable(str(exc)) from exc


__all__ = [
    "InvalidRegistryConfig",
    "StatusSpecUnreadable",
    "load_status_config",
    "resolve_status_spec",
]
