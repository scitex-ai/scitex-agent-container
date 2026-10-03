"""Honor an explicitly denied automatic Store footprint at launch."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

_DENIED_ENV = ("SCITEX_STORE_DSN", "PGUSER", "PGPASSFILE")
_NEUTRALIZER_DESTINATIONS = frozenset(
    {
        "/home/agent/.scitex/cards",
        "/home/agent/.scitex/claude-code-telegrammer",
        "/tmp/emacs-claude-code",
        "/tmp/host",
    }
)


class IsolationFootprintError(RuntimeError):
    """A declared read-only neutralizer cannot preserve the denied footprint."""

    kind = "isolation-footprint"


def automatic_store_denied(config: object) -> bool:
    """Only three present, literal empty declarations deny automatic authority.

    The loaded env contains the spec's declarations, before fleet defaults.
    Missing variables, whitespace and absent configs do not express denial.
    """
    env = getattr(config, "env", None)
    return isinstance(env, Mapping) and all(
        name in env and type(env[name]) is str and env[name] == ""
        for name in _DENIED_ENV
    )


def validate_neutralizer_binds(config: object) -> None:
    """Check declared neutralizers without creating or repairing any path."""
    if not automatic_store_denied(config):
        return
    apptainer = getattr(config, "apptainer", None)
    for binding in getattr(apptainer, "binds", ()) or ():
        parts = str(binding).split(":")
        if len(parts) != 3 or parts[2] != "ro":
            continue  # An explicit authority mount keeps its declared meaning.
        source, destination = parts[:2]
        if destination.rstrip("/") not in _NEUTRALIZER_DESTINATIONS:
            continue
        path = Path(source).expanduser()
        try:
            if not path.is_dir():
                reason = "neutralizer-missing-or-not-directory"
            elif next(path.iterdir(), None) is not None:
                reason = "neutralizer-not-empty"
            else:
                continue
        except OSError:
            reason = "neutralizer-unverifiable"
        raise IsolationFootprintError(
            f"[sac-isolation-footprint] {reason}: agent "
            f"{getattr(config, 'name', '<unknown>')!r}, spec "
            f"{getattr(config, 'config_path', '<unknown>')!r}, declared "
            f"source {source!r} -> {destination!r}. Verify the declared empty "
            "directory on this host; do not add shared capabilities or retry "
            "unchanged. No path was repaired."
        )
