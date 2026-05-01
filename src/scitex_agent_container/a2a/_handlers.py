"""Pluggable A2A ``tasks/send`` handlers for sac.

Four built-ins:

* :func:`handle_echo` — canned reply, zero deps. The default.
* :func:`handle_claude_session` — drives Claude via Anthropic's
  official ``claude-agent-sdk`` (structured streaming, no ``--print``).
  **Recommended** for new agents — survives ``--print`` deprecation.
* :func:`handle_claude_cli` — *(legacy)* runs ``claude --print`` with
  the user text and forwards stdout. Kept for back-compat; will be
  removed once ``claude --print`` itself is removed upstream. Migrate
  to ``claude_session``.
* :func:`handle_exec` — runs an arbitrary command, passing the user
  text on stdin and returning stdout. Lets ops wire in any custom
  handler script (Python, shell, etc.) without sac changes.

All handlers take ``(agent_name, user_text)`` and return the agent's
reply string. Errors raise :class:`HandlerError`; the server wraps
them into a JSON-RPC error envelope.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Callable

CLAUDE_TIMEOUT_S = float(os.environ.get("SAC_A2A_CLAUDE_TIMEOUT_S", "25"))
EXEC_TIMEOUT_S = float(os.environ.get("SAC_A2A_EXEC_TIMEOUT_S", "25"))

CLAUDE_DEFAULT_SYSTEM = (
    "You are a brief responder. Reply to the user in one or two short "
    "sentences. Do not run any tools, do not ask follow-up questions."
)


class HandlerError(RuntimeError):
    """Raised by an A2A handler to signal a recoverable failure."""


def handle_echo(agent_name: str, user_text: str) -> str:
    """Canned reply — proves the protocol surface without external deps."""
    return f"[{agent_name}] received {user_text!r} (sac echo handler)."


def handle_claude_cli(agent_name: str, user_text: str) -> str:
    """Run ``claude --print`` once with ``user_text``, return stdout."""
    claude_bin = os.environ.get("SAC_A2A_CLAUDE_BIN", "claude")
    system = os.environ.get("SAC_A2A_CLAUDE_SYSTEM", CLAUDE_DEFAULT_SYSTEM)
    cmd = [claude_bin, "--print", "--append-system-prompt", system]
    model = os.environ.get("SAC_A2A_CLAUDE_MODEL")
    if model:
        cmd.extend(["--model", model])
    try:
        res = subprocess.run(
            cmd,
            input=user_text,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except (
        FileNotFoundError
    ) as exc:  # stx-allow: fallback (reason: file may not exist on first use)
        raise HandlerError(f"claude CLI not found at {claude_bin!r}") from exc
    except (
        subprocess.TimeoutExpired
    ) as exc:  # stx-allow: fallback (reason: subprocess execution failure)
        raise HandlerError(f"claude CLI timeout after {CLAUDE_TIMEOUT_S:.0f}s") from exc
    if res.returncode != 0:
        raise HandlerError(
            f"claude CLI failed (rc={res.returncode}): {res.stderr.strip()[:300]}"
        )
    return res.stdout.strip() or "(empty response)"


def _agent_mcp_servers_and_cwd(agent_name: str) -> tuple[dict, str | None]:
    """Resolve the agent's MCP servers + workspace cwd from the registry.

    Returns ``(mcp_servers_dict, cwd_or_None)``. Empty dict + None if the
    agent isn't registered or its workspace has no ``.mcp.json``. This
    lets ``claude_session`` requests reach the same MCP servers (orochi,
    custom stdio, etc.) the agent's interactive Claude Code session would.
    """
    import json as _json
    import re as _re
    from pathlib import Path as _Path

    try:
        from scitex_agent_container.registry import Registry
    except ImportError:
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

    # Workdir lives next to the agent's runtime workspace; sac writes
    # `.mcp.json` there at startup via runtimes/mcp_config.py.
    try:
        from scitex_agent_container.config import load_config

        cfg = load_config(config_path)
        workdir = str(_Path(cfg.expanded_workdir).expanduser())
    except Exception:  # stx-allow: fallback (reason: config load best-effort)
        return {}, None

    mcp_path = _Path(workdir) / ".mcp.json"
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

    # Resolve `${VAR}` references against this process's environment so
    # tokens (SCITEX_OROCHI_TOKEN, etc.) reach the spawned MCP processes.
    def _resolve_env(value):
        if isinstance(value, str):
            return _re.sub(
                r"\$\{(\w+)\}",
                lambda m: os.environ.get(m.group(1), m.group(0)),
                value,
            )
        if isinstance(value, dict):
            return {k: _resolve_env(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_resolve_env(v) for v in value]
        return value

    resolved: dict = {}
    for name, entry_dict in mcp_servers.items():
        if not isinstance(entry_dict, dict):
            continue
        # SDK expects `type` to be present (or absent for stdio); the
        # on-disk .mcp.json may or may not have it. Default to stdio.
        e = _resolve_env(dict(entry_dict))
        e.setdefault("type", "stdio")
        resolved[name] = e
    return resolved, workdir


def handle_claude_session(agent_name: str, user_text: str) -> str:
    """Drive Claude via ``claude-agent-sdk`` — no ``claude --print``.

    Same wire contract as :func:`handle_claude_cli` (sync ``(name, text)
    -> str``), but uses Anthropic's official SDK underneath. The SDK
    speaks structured streaming on stdin/stdout to a long-lived ``claude``
    process and survives the eventual ``--print`` deprecation.

    MCP wiring: looks up the agent's workspace ``.mcp.json`` (written by
    sac at agent-start time from ``spec.mcp_servers``) and passes it
    through to the SDK. So an agent whose YAML declares the
    ``scitex-orochi`` MCP server can answer A2A requests AND speak
    orochi from the same handler call. ``${VAR}`` references in the
    on-disk JSON are resolved against this process's environment.

    Env knobs:

    * ``SAC_A2A_CLAUDE_MODEL`` — model id (default: SDK default).
    * ``SAC_A2A_CLAUDE_SYSTEM`` — appended system prompt (default:
      :data:`CLAUDE_DEFAULT_SYSTEM`).
    * ``SAC_A2A_CLAUDE_TIMEOUT_S`` — per-call timeout in seconds
      (default 25; bump for prompts that read multiple files / call MCP).

    Auth: the bundled CLI honors ``ANTHROPIC_API_KEY`` (canonical, ToS-
    clean) and falls back to ``~/.claude/.credentials.json`` OAuth on
    personal machines. See Anthropic's commercial ToS for redistributed
    products.
    """
    try:
        import asyncio
        import warnings as _warnings

        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )
    except ImportError as exc:  # stx-allow: fallback (reason: optional dep at runtime)
        raise HandlerError(
            "claude_session handler requires `claude-agent-sdk` "
            "(`pip install claude-agent-sdk`)."
        ) from exc

    system = os.environ.get("SAC_A2A_CLAUDE_SYSTEM", CLAUDE_DEFAULT_SYSTEM)
    model = os.environ.get("SAC_A2A_CLAUDE_MODEL")
    mcp_servers, workdir = _agent_mcp_servers_and_cwd(agent_name)

    options_kwargs: dict = {"system_prompt": system}
    if model:
        options_kwargs["model"] = model
    if workdir:
        options_kwargs["cwd"] = workdir
    if mcp_servers:
        options_kwargs["mcp_servers"] = mcp_servers
    options = ClaudeAgentOptions(**options_kwargs)

    chunks: list[str] = []

    async def _consume() -> None:
        async for msg in query(prompt=user_text, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)

    try:
        # Suppress noisy SDK / asyncio shutdown warnings per call.
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            asyncio.run(asyncio.wait_for(_consume(), timeout=CLAUDE_TIMEOUT_S))
    except asyncio.TimeoutError as exc:
        raise HandlerError(
            f"claude_session timeout after {CLAUDE_TIMEOUT_S:.0f}s"
        ) from exc
    except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad)
        raise HandlerError(f"claude_session failed: {exc}") from exc

    return "\n".join(chunks).strip() or "(empty response)"


def handle_exec(agent_name: str, user_text: str) -> str:
    """Run ``$SAC_A2A_EXEC_COMMAND``, piping user_text on stdin."""
    raw = os.environ.get("SAC_A2A_EXEC_COMMAND", "")
    if not raw.strip():
        raise HandlerError(
            "SAC_A2A_EXEC_COMMAND is not set; can't dispatch via 'exec' handler"
        )
    try:
        argv = shlex.split(raw)
    except (
        ValueError
    ) as exc:  # stx-allow: fallback (reason: type coercion or format mismatch)
        raise HandlerError(f"could not parse SAC_A2A_EXEC_COMMAND: {exc}") from exc
    if not argv:
        raise HandlerError("SAC_A2A_EXEC_COMMAND parsed to empty argv")
    try:
        res = subprocess.run(
            argv,
            input=user_text,
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT_S,
            env={**os.environ, "SAC_A2A_AGENT": agent_name},
        )
    except (
        FileNotFoundError
    ) as exc:  # stx-allow: fallback (reason: file may not exist on first use)
        raise HandlerError(f"exec command not found: {argv[0]!r}") from exc
    except (
        subprocess.TimeoutExpired
    ) as exc:  # stx-allow: fallback (reason: subprocess execution failure)
        raise HandlerError(f"exec command timeout after {EXEC_TIMEOUT_S:.0f}s") from exc
    if res.returncode != 0:
        raise HandlerError(
            f"exec command failed (rc={res.returncode}): {res.stderr.strip()[:300]}"
        )
    return res.stdout.strip() or "(empty response)"


HANDLERS: dict[str, Callable[[str, str], str]] = {
    "echo": handle_echo,
    "claude_session": handle_claude_session,  # recommended (SDK-backed)
    "claude_cli": handle_claude_cli,  # legacy (--print, deprecating upstream)
    "exec": handle_exec,
}
