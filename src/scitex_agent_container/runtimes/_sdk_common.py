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

# ---------------------------------------------------------------------------
# Why we never honour a pre-set ANTHROPIC_API_KEY
# ---------------------------------------------------------------------------
# The Anthropic SDK auto-reads ``ANTHROPIC_API_KEY`` from the process
# env. That auto-pickup is hostile to sac because:
#
#   1. **Dotfiles drift.** Operators historically exported stale API
#      keys (or OAuth bearers under various names) from ``.bashrc``.
#      An expired value silently survives every shell, every container
#      bind-mounting the parent env, and every CI runner that inherits
#      the secret. Symptom in production: "401 Invalid auth" or
#      "Command failed exit 1" with no obvious cause — the SDK
#      preferred the env var over the working OAuth credentials file.
#
#   2. **Surprise pay-per-token billing.** A pre-set
#      ``ANTHROPIC_API_KEY`` shadows the Pro/Max flat-rate OAuth path
#      in the credentials file. Operators paying for Pro/Max suddenly
#      see "Credit balance is too low" because the SDK quietly
#      switched to API-key billing.
#
#   3. **Provenance.** sac wants ONE tracked source of truth for the
#      key. If an operator can side-load a value via
#      ``ANTHROPIC_API_KEY``, every audit / quota / log lies about
#      where the credential came from.
#
# The contract is therefore: **``SAC_ANTHROPIC_API_KEY`` is the only
# env input we honour.** Whenever this function runs we:
#
#   * If ``SAC_ANTHROPIC_API_KEY`` is set → unconditionally OVERWRITE
#     ``ANTHROPIC_API_KEY`` with it (highest priority, no fallback).
#   * If ``SAC_ANTHROPIC_API_KEY`` is unset → POP ``ANTHROPIC_API_KEY``
#     from the env so a stale dotfiles export can't be picked up by
#     the SDK auto-reader after we return.
#
# Then the credentials-file path takes precedence (Pro/Max flat-rate),
# falling back to the SAC-provided env value (api-key form bridged
# directly; OAuth form synthesised into a credentials.json).
# ---------------------------------------------------------------------------

_SAC_API_KEY_ENV = "SAC_ANTHROPIC_API_KEY"


class SDKCommonError(RuntimeError):
    """Raised when the SDK common helpers cannot satisfy a precondition."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def provision_anthropic_auth() -> str:
    """Make sure the SDK can authenticate; return the path that will be used.

    Auth flow is *one-directional*:

        ``~/.claude/.credentials.json``  →  ``SAC_ANTHROPIC_API_KEY``
                                            (extracted by ``sac dev
                                            extract-apikey-from-credentials`` or the
                                            bash bridge)
        ``SAC_ANTHROPIC_API_KEY``        →  ``ANTHROPIC_API_KEY``
                                            (overridden here)

    Sac NEVER writes/synthesises ``credentials.json``. It is treated
    as a read-only artefact produced by ``claude /login``.

    Step 1 (always): ``SAC_ANTHROPIC_API_KEY`` overrides
    ``ANTHROPIC_API_KEY``. If SAC is unset, ``ANTHROPIC_API_KEY`` is
    popped. See the module-level comment for *why*.

    Step 2: pick a path, in precedence order:

    1. ``~/.claude/.credentials.json`` exists → ``"credentials_file"``
       (Pro/Max OAuth, flat-rate; SDK reads the file directly).
    2. ``SAC_ANTHROPIC_API_KEY`` set → ``"sac_env"`` (already mirrored
       to ``ANTHROPIC_API_KEY`` in step 1; the SDK reads it as-is).
    3. Neither → :class:`SDKCommonError`.
    """
    # Step 1 — SAC value is the only trusted env source. Override or pop.
    sac_value = os.environ.get(_SAC_API_KEY_ENV)
    if sac_value:
        os.environ["ANTHROPIC_API_KEY"] = sac_value
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    # Step 2 — pick the auth path. Cred-file is preferred; SAC env is fallback.
    if _CRED_FILE.is_file():
        return "credentials_file"
    if sac_value:
        return "sac_env"
    raise SDKCommonError(
        f"no Anthropic auth available — run `claude /login` so "
        f"{_CRED_FILE} exists, or export {_SAC_API_KEY_ENV}. "
        "sac does NOT honour a pre-set ANTHROPIC_API_KEY (see the "
        "module-level comment in runtimes/_sdk_common.py for why), "
        "and never writes/synthesises credentials.json itself."
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
