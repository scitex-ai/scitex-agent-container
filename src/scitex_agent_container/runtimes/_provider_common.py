"""Provider-agnostic session-workspace helpers.

Extracted from ``runtimes/_sdk_common.py`` (openai-compat-1 foundation —
scitex-todo card ``openai-compat-1``, "Land ProviderConfig + ProviderSession
Protocol"). ``_sdk_common.py`` bundled three concerns: auth provisioning,
workspace/MCP resolution, and ``ClaudeAgentOptions`` composition. Of those,
workspace/MCP resolution never referenced ``claude_agent_sdk`` or any other
Anthropic-specific type — ANY provider's runner needs to resolve an agent's
workspace ``cwd`` and its ``.mcp.json`` server map the same way. That
provider-agnostic core now lives HERE so the ``openai`` runner
(openai-compat-2's ``runtimes/_openai_sdk_common.py``) can reuse it verbatim
instead of duplicating it.

``_sdk_common.py`` re-exports every public name below (unchanged import
paths for every existing caller) and layers the Claude/Anthropic-SPECIFIC
concerns (auth provisioning, ``ClaudeAgentOptions`` composition) on top of
it. This is a pure, byte-identical-behavior EXTRACTION, not a rewrite —
see the module docstring of ``_sdk_common.py`` for the full concern split.

Nothing in this module imports ``claude_agent_sdk`` (even lazily) or any
other provider SDK — that is the whole point of the split.
"""

from __future__ import annotations

import json as _json
import os
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ..config._types import AgentConfig

__all__ = [
    "project_runtime_root",
    "resolve_agent_workspace",
]


def project_runtime_root(config: "AgentConfig") -> "Path | None":
    """If the agent's YAML lives under a project-scope
    ``.scitex/agent-container/`` tree, return the sibling ``runtime/``
    so per-agent state lands inside the same repo. Otherwise None.

    In-repo test agents get in-repo state, keeping ``~/.scitex`` clean
    and letting CI snapshot transcripts as build artifacts.
    """
    src = getattr(config, "config_path", "") or ""
    if not src:
        return None
    try:
        from scitex_config._ecosystem import local_state
    except Exception:  # stx-allow: fallback (reason: scitex-config optional; degrade to home-scope state)
        return None
    scope = local_state.find_project_scope("agent-container", start=Path(src).parent)
    return (scope / "runtime") if scope is not None else None


def _resolve_env_refs(value: Any) -> Any:
    """Substitute ``${VAR}`` references against the current process env.

    Strings, dicts, and lists are walked recursively. Any other type is
    passed through unchanged. Unresolved references are left literal so
    misconfigurations are visible at the SDK call rather than silently
    becoming empty strings.
    """
    if isinstance(value, str):
        return _re.sub(
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(v) for v in value]
    return value


def resolve_agent_workspace(agent_name: str) -> tuple[dict, str | None]:
    """Resolve ``(mcp_servers, cwd)`` for a registered agent.

    Returns ``({}, None)`` if the agent isn't registered or its workspace
    has no ``.mcp.json``. Best-effort: any IO / parse failure produces
    the empty result rather than raising — the caller (option-builder)
    decides what to do with an unknown workspace.

    The ``mcp_servers`` dict matches the SDK's expected shape: each
    entry has ``type`` defaulted to ``"stdio"`` if absent, and any
    ``${VAR}`` references resolved.
    """
    try:
        from scitex_agent_container._state.registry import Registry
    except Exception:  # stx-allow: fallback (reason: optional dep at runtime; broaden beyond ImportError so a misbuilt transitive dep can't crash the option-builder)
        return {}, None

    try:
        entry = Registry().get(agent_name)
    except Exception:  # stx-allow: fallback (reason: registry IO best-effort)
        return {}, None
    if not entry:
        return {}, None
    config_path = entry.get("config")
    if not config_path:
        return {}, None

    try:
        from scitex_agent_container.config import load_config

        cfg = load_config(config_path)
        workdir = str(Path(cfg.expanded_workdir).expanduser())
    except Exception:  # stx-allow: fallback (reason: config load best-effort)
        return {}, None

    mcp_path = Path(workdir) / ".mcp.json"
    if not mcp_path.is_file():
        return {}, workdir
    try:
        raw = _json.loads(mcp_path.read_text(encoding="utf-8"))
    except (
        OSError,
        _json.JSONDecodeError,
    ):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return {}, workdir

    mcp_servers = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
    if not isinstance(mcp_servers, dict):
        return {}, workdir

    resolved: dict = {}
    for name, entry_dict in mcp_servers.items():
        if not isinstance(entry_dict, dict):
            continue
        e = _resolve_env_refs(dict(entry_dict))
        e.setdefault("type", "stdio")
        resolved[name] = e
    return resolved, workdir
