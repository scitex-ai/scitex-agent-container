"""Pluggable A2A ``tasks/send`` handlers for sac.

Five built-ins:

* :func:`handle_echo` — canned reply, zero deps. The default.
* :func:`handle_claude_session` — drives Claude via Anthropic's
  official ``claude-agent-sdk`` (structured streaming, no ``--print``).
  **Recommended** for new agents — survives ``--print`` deprecation.
* :func:`handle_openai_session` — drives an OpenAI model via the
  ``openai-agents`` SDK (``spec.provider: openai`` family; optional
  ``[openai]`` extra). Stateful: turns share the agent's
  ``SQLiteSession`` conversation state.
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

from scitex_agent_container._env import getenv as _sac_env

CLAUDE_TIMEOUT_S = float(_sac_env("A2A_CLAUDE_TIMEOUT_S", "25"))
OPENAI_TIMEOUT_S = float(_sac_env("A2A_OPENAI_TIMEOUT_S", "25"))
EXEC_TIMEOUT_S = float(_sac_env("A2A_EXEC_TIMEOUT_S", "25"))

# sac does NOT inject a default system prompt. The agent's persona +
# rules belong to the operator's yaml / `~/.claude/CLAUDE.md`, not to
# this framework. Set `SAC_A2A_CLAUDE_SYSTEM` only if you explicitly
# want sac to override the operator's prompt for the A2A turn path.
#
# Prior versions of this file hardcoded `"Do not run any tools"` in
# the default — that silently neutered `spec.claude.channels:
# [server:sac]` (tools registered, then refused). Removed wholesale:
# no surprise prompts in production.


class HandlerError(RuntimeError):
    """Raised by an A2A handler to signal a recoverable failure."""


def handle_echo(agent_name: str, user_text: str) -> str:
    """Canned reply — proves the protocol surface without external deps."""
    return f"[{agent_name}] received {user_text!r} (sac echo handler)."


def handle_claude_cli(agent_name: str, user_text: str) -> str:
    """Run ``claude --print`` once with ``user_text``, return stdout."""
    claude_bin = _sac_env("A2A_CLAUDE_BIN", "claude")
    system = _sac_env("A2A_CLAUDE_SYSTEM", "").strip()
    cmd = [claude_bin, "--print"]
    if system:
        cmd.extend(["--append-system-prompt", system])
    model = _sac_env("A2A_CLAUDE_MODEL")
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


def handle_claude_session(
    agent_name: str,
    user_text: str,
    *,
    channels: list[str] | None = None,
    a2a_port: int | None = None,
    permission_mode: str | None = None,
) -> str:
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
    * ``SAC_A2A_CLAUDE_SYSTEM`` — optional explicit override for the
      A2A turn's system prompt. Unset by default; sac no longer
      injects any framework-side prompt.
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

    # No framework-injected system prompt. Operators wanting an A2A-turn
    # override can set SAC_A2A_CLAUDE_SYSTEM explicitly; otherwise we
    # pass None and the agent uses its own configured persona.
    system = _sac_env("A2A_CLAUDE_SYSTEM", "").strip() or None
    model = _sac_env("A2A_CLAUDE_MODEL")
    # Forward `spec.claude.channels` + the agent's own A2A port. The sac
    # MCP sidecar (auto-injected when `server:sac` is in channels)
    # subscribes to the inbox SSE on the BUS (`sac listen`, resolved from
    # SAC_LISTEN_BASE_URL), not the a2a_port. a2a_port is forwarded only so
    # build_sdk_options has it for the /v1/turn registration path.
    sdk_extra: dict | None = None
    if channels or a2a_port is not None:
        sdk_extra = {}
        if channels:
            sdk_extra["_channels"] = list(channels)
        if a2a_port is not None:
            sdk_extra["_a2a_port"] = int(a2a_port)
    try:
        options = build_sdk_options(
            agent_name,
            system_prompt=system,
            model=model,
            permission_mode=permission_mode,
            extra=sdk_extra,
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


def handle_openai_session(agent_name: str, user_text: str) -> str:
    """Drive an OpenAI model via the ``openai-agents`` SDK.

    Same sync wire contract as :func:`handle_claude_session`
    (``(name, text) -> str``), backed by
    :class:`scitex_agent_container._runners.openai_session.OpenAISession`
    (the concrete ``ProviderSession`` — see openai-compat-2). Unlike the
    Claude handler this one is STATEFUL: the session persists turns in
    the agent's ``SQLiteSession`` db (see
    ``runtimes._openai_sdk_common.resolve_state_db_path``), so repeated
    A2A sends continue one conversation.

    Env knobs (mirroring the Claude handler's):

    * ``SAC_A2A_OPENAI_MODEL`` — model id (default: ``SAC_OPENAI_MODEL``
      → the SDK default).
    * ``SAC_A2A_OPENAI_SYSTEM`` — optional system prompt override.
    * ``SAC_A2A_OPENAI_TIMEOUT_S`` — per-call timeout (default 25).

    Auth: ``SAC_OPENAI_API_KEY`` (preferred) → ``OPENAI_API_KEY`` — see
    ``runtimes._openai_sdk_common.provision_openai_auth`` for the
    contract (and its documented asymmetry with the Anthropic path).
    """
    import asyncio

    try:
        import agents  # noqa: F401 — availability probe only
    except ImportError as exc:  # stx-allow: fallback (reason: optional dep at runtime)
        raise HandlerError(
            "openai_session handler requires `openai-agents` "
            "(`pip install scitex-agent-container[openai]`)."
        ) from exc

    from scitex_agent_container._runners._provider_session import Message
    from scitex_agent_container._runners.openai_session import (
        OpenAISession,
        OpenAISessionError,
    )
    from scitex_agent_container.runtimes._openai_sdk_common import (
        OpenAISDKCommonError,
    )

    system = _sac_env("A2A_OPENAI_SYSTEM", "").strip() or None
    model = _sac_env("A2A_OPENAI_MODEL") or None

    async def _drive() -> str:
        session = OpenAISession(agent_name, model=model, instructions=system)
        await session.start()
        try:
            reply = ""
            deltas: list[str] = []
            message = Message(role="user", content=user_text)
            async for event in session.send(message):
                if event.kind == "text_delta":
                    deltas.append(event.text)
                elif event.kind == "result" and event.result is not None:
                    reply = event.result.text
                elif event.kind == "error":
                    raise HandlerError(f"openai_session failed: {event.error}")
            return reply or "".join(deltas)
        finally:
            await session.close()

    try:
        return (
            asyncio.run(asyncio.wait_for(_drive(), timeout=OPENAI_TIMEOUT_S)).strip()
            or "(empty response)"
        )
    except HandlerError:
        raise
    except (OpenAISessionError, OpenAISDKCommonError) as exc:
        raise HandlerError(str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HandlerError(
            f"openai_session timeout after {OPENAI_TIMEOUT_S:.0f}s"
        ) from exc
    except Exception as exc:  # stx-allow: fallback (reason: SDK surface is broad)
        raise HandlerError(f"openai_session failed: {exc}") from exc


def handle_exec(agent_name: str, user_text: str) -> str:
    """Run ``$SAC_A2A_EXEC_COMMAND``, piping user_text on stdin."""
    raw = _sac_env("A2A_EXEC_COMMAND", "")
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
    "openai_session": handle_openai_session,  # openai-agents SDK ([openai] extra)
    "claude_cli": handle_claude_cli,  # legacy (--print, deprecating upstream)
    "exec": handle_exec,
}
