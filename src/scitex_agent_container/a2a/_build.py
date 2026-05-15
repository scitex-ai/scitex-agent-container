"""YAML loading + executor selection for ``sac a2a serve``.

Extracted from ``_server.py`` to keep that file under the project's
per-file line limit. Every helper here is a pure function of the yaml
dict and the on-disk path — no Starlette / SDK dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scitex_agent_container.a2a._handlers import HANDLERS
from scitex_agent_container.a2a.executors import EXECUTORS, BaseSyncExecutor


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def agent_name_from_yaml(path: Path, v3: dict[str, Any]) -> str:
    """Resolve an agent's name from its yaml + on-disk path.

    Precedence: ``metadata.name`` → dir-as-SSoT (parent dir name when
    file is ``spec.yaml``) → file stem. Raises if the file is
    ``spec.yaml`` with no parent dir to disambiguate — that's a real
    error, not a fallback (two ``spec.yaml`` siblings would silently
    collide on the file stem).
    """
    meta = v3.get("metadata") or {}
    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    if path.stem == "spec":
        if not path.parent.name:
            raise ValueError(
                f"cannot derive agent name from {path}: file is 'spec.yaml' "
                "but parent dir is empty. Use dir-as-SSoT layout "
                "(agents/<name>/spec.yaml) or set metadata.name in the yaml."
            )
        return path.parent.name
    return path.stem


def select_handler_key(v3: dict[str, Any], default: str) -> str:
    """Read ``spec.a2a.handler`` from the v3 yaml (falling back to ``default``)."""
    a2a_block = (v3.get("spec") or {}).get("a2a") or {}
    key = a2a_block.get("handler")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return default


def select_permission_mode(claude_block: dict[str, Any]) -> str | None:
    """Map ``spec.claude.permission_mode`` (preferred) or the equivalent
    legacy ``spec.claude.flags: [--dangerously-skip-permissions]`` form
    to the SDK's permission_mode field.

    Without this the A2A claude_session handler runs in `default` mode
    and silently rejects MCP tool calls (the model sees the registered
    tool as not-available). The yaml's intent must be honored.
    """
    explicit = claude_block.get("permission_mode")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    flags = claude_block.get("flags") or []
    if isinstance(flags, list) and any(
        isinstance(f, str) and f.strip() == "--dangerously-skip-permissions"
        for f in flags
    ):
        return "bypassPermissions"
    return None


def build_executor(
    name: str,
    handler_key: str,
    v3: dict[str, Any],
    a2a_port: int | None,
) -> BaseSyncExecutor:
    """Construct an executor with per-agent context (yaml + a2a port).

    ``v3``, ``a2a_port``, channels, and the resolved permission_mode are
    surfaced via ``BaseSyncExecutor.kwargs`` so executors like
    :class:`ClaudeSessionExecutor` can thread them through to
    :func:`build_sdk_options`.
    """
    cls = EXECUTORS.get(handler_key)
    if cls is None:
        raise ValueError(
            f"unknown a2a handler {handler_key!r}; pick one of {sorted(EXECUTORS)}"
        )
    if handler_key not in HANDLERS:
        raise ValueError(
            f"agent {name!r}: unknown a2a handler {handler_key!r}; "
            f"pick one of {sorted(HANDLERS)}"
        )
    claude_block = (v3.get("spec") or {}).get("claude") or {}
    channels = list(claude_block.get("channels") or [])
    permission_mode = select_permission_mode(claude_block)
    return cls(
        agent_name=name,
        channels=channels,
        a2a_port=a2a_port,
        permission_mode=permission_mode,
    )


__all__ = [
    "agent_name_from_yaml",
    "build_executor",
    "load_yaml",
    "select_handler_key",
    "select_permission_mode",
]
