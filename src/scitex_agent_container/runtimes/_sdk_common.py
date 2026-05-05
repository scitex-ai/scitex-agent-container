"""Shared helpers for the ``claude-agent-sdk`` runtime path.

This module is the single source of truth for three concerns that any
SDK-backed code path needs:

1. **Auth provisioning** — :func:`provision_anthropic_auth` ensures the
   SDK can authenticate without forcing the pay-per-token API-key path.
   Production agents run on Pro/Max **OAuth** (flat-rate); we bridge a
   stored OAuth token into ``ANTHROPIC_API_KEY`` only when neither
   ``ANTHROPIC_API_KEY`` nor ``~/.claude/.credentials.json`` is already
   present (headless contexts: SLURM, CI, fresh containers).

2. **Workspace resolution** — :func:`resolve_agent_workspace` reads the
   running agent's registry entry, computes its workspace ``cwd``, and
   parses the on-disk ``.mcp.json`` (already materialized by sac at
   agent-start time from ``spec.mcp_servers``) into the SDK's expected
   shape, with ``${VAR}`` references resolved against the current
   process environment.

3. **Options building** — :func:`build_sdk_options` composes the result
   of the previous two with per-caller knobs (system prompt, model,
   permission mode, hooks) into a ``ClaudeAgentOptions`` dataclass.

Both the existing one-shot A2A handler
(:func:`scitex_agent_container.a2a._handlers.handle_claude_session`)
and the upcoming long-lived ``claude-session`` runtime consume the same
helpers — guaranteeing that MCP wiring, auth, and model resolution
behave identically across the request/response path and the
lifecycle path.

The ``claude-agent-sdk`` import is **lazy**: importing this module does
not require the SDK to be installed. Each public helper imports the SDK
on demand and raises a clear error otherwise.
"""

from __future__ import annotations

import json as _json
import os
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from claude_agent_sdk import ClaudeAgentOptions

__all__ = [
    "SDKCommonError",
    "provision_anthropic_auth",
    "resolve_agent_workspace",
    "build_sdk_options",
]

_CRED_FILE = Path.home() / ".claude" / ".credentials.json"

# Sac-managed handoff env. The host sets SAC_ANTHROPIC_API_KEY (or sac
# forwards it from the operator's env into the container); the runner
# translates it to ANTHROPIC_API_KEY for the SDK transport. ANTHROPIC_API_KEY
# itself is only honored when the user set it explicitly.
_SAC_API_KEY_ENV = "SAC_ANTHROPIC_API_KEY"


class SDKCommonError(RuntimeError):
    """Raised when the SDK common helpers cannot satisfy a precondition."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def provision_anthropic_auth() -> str:
    """Make sure the SDK can authenticate; return the path that will be used.

    Decision order (first applicable wins):

    1. ``ANTHROPIC_API_KEY`` already set in the environment → ``"env"``
       (operator opted in explicitly; we don't second-guess).
    2. ``~/.claude/.credentials.json`` exists → ``"credentials_file"``
       (Pro/Max OAuth token; SDK reads the file directly).
    3. ``SAC_ANTHROPIC_API_KEY`` set → bridge to ANTHROPIC_API_KEY
       (``"bridged_sac"``). This is the standard sac-managed path —
       operator hands sac the key under a sac-namespaced var; sac
       translates only when actually launching the SDK runner.

    Raises :class:`SDKCommonError` if none of the above apply.

    Idempotent: calling repeatedly within the same process is safe and
    returns the same path (after the first call, ``ANTHROPIC_API_KEY``
    is set if a bridge happened, so subsequent calls return ``"env"``).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    if _CRED_FILE.is_file():
        return "credentials_file"
    bridged = os.environ.get(_SAC_API_KEY_ENV)
    if bridged:
        os.environ["ANTHROPIC_API_KEY"] = bridged
        return "bridged_sac"
    raise SDKCommonError(
        "no Anthropic auth available — set ANTHROPIC_API_KEY, run a "
        "Pro/Max claude /login (~/.claude/.credentials.json), or export "
        f"{_SAC_API_KEY_ENV}"
    )


# ---------------------------------------------------------------------------
# Workspace + MCP wiring
# ---------------------------------------------------------------------------


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
    except ImportError:  # stx-allow: fallback (reason: optional dep at runtime)
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


# ---------------------------------------------------------------------------
# Options builder
# ---------------------------------------------------------------------------


def build_sdk_options(
    agent_name: str,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    hooks: dict | None = None,
    resume: str | None = None,
    extra: dict | None = None,
) -> "ClaudeAgentOptions":
    """Compose a ``ClaudeAgentOptions`` for ``agent_name``.

    Calls :func:`provision_anthropic_auth` (so callers don't have to
    sequence it themselves) and :func:`resolve_agent_workspace` (so the
    agent's MCP servers and workspace cwd are wired automatically).
    Per-caller knobs (``system_prompt``, ``model``, ``permission_mode``,
    ``hooks``, ``resume``) layer on top. ``extra`` is a dict of any
    other supported ``ClaudeAgentOptions`` field — used sparingly for
    forward-compat with new SDK options.

    Raises :class:`SDKCommonError` if the SDK is not installed or no
    auth path is available.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ImportError as exc:  # stx-allow: fallback (reason: optional dep at runtime)
        raise SDKCommonError(
            "claude-agent-sdk is not installed (`pip install claude-agent-sdk`)"
        ) from exc

    provision_anthropic_auth()
    mcp_servers, workdir = resolve_agent_workspace(agent_name)

    kwargs: dict = {}
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    if model:
        kwargs["model"] = model
    if permission_mode:
        kwargs["permission_mode"] = permission_mode
    if hooks:
        kwargs["hooks"] = hooks
    if resume:
        kwargs["resume"] = resume
    if workdir:
        kwargs["cwd"] = workdir
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    if extra:
        kwargs.update(extra)

    return ClaudeAgentOptions(**kwargs)
