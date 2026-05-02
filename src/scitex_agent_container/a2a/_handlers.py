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


# Backwards-compat alias: this private helper was relocated to
# ``runtimes/_sdk_common.py::resolve_agent_workspace``. Old call sites
# (and downstream forks that grepped for it) keep working.
def _agent_mcp_servers_and_cwd(agent_name: str) -> tuple[dict, str | None]:
    """(Deprecated) Resolve agent's MCP servers + workspace cwd.

    Forwards to :func:`scitex_agent_container.runtimes._sdk_common.resolve_agent_workspace`.
    """
    from scitex_agent_container.runtimes._sdk_common import resolve_agent_workspace

    return resolve_agent_workspace(agent_name)


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
            TextBlock,
            query,
        )
    except ImportError as exc:  # stx-allow: fallback (reason: optional dep at runtime)
        raise HandlerError(
            "claude_session handler requires `claude-agent-sdk` "
            "(`pip install claude-agent-sdk`)."
        ) from exc

    from scitex_agent_container.runtimes._sdk_common import (
        SDKCommonError,
        build_sdk_options,
    )

    system = os.environ.get("SAC_A2A_CLAUDE_SYSTEM", CLAUDE_DEFAULT_SYSTEM)
    model = os.environ.get("SAC_A2A_CLAUDE_MODEL")
    try:
        options = build_sdk_options(
            agent_name,
            system_prompt=system,
            model=model,
        )
    except SDKCommonError as exc:
        raise HandlerError(str(exc)) from exc

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
