"""Pluggable A2A ``tasks/send`` handlers for sac.

Three built-ins:

* :func:`handle_echo` — canned reply, zero deps. The default.
* :func:`handle_claude_cli` — runs ``claude --print`` with the user
  text and forwards stdout. Requires ``claude`` on PATH.
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
    except FileNotFoundError as exc:  # stx-allow: fallback (reason: file may not exist on first use)
        raise HandlerError(f"claude CLI not found at {claude_bin!r}") from exc
    except subprocess.TimeoutExpired as exc:  # stx-allow: fallback (reason: subprocess execution failure)
        raise HandlerError(f"claude CLI timeout after {CLAUDE_TIMEOUT_S:.0f}s") from exc
    if res.returncode != 0:
        raise HandlerError(
            f"claude CLI failed (rc={res.returncode}): {res.stderr.strip()[:300]}"
        )
    return res.stdout.strip() or "(empty response)"


def handle_exec(agent_name: str, user_text: str) -> str:
    """Run ``$SAC_A2A_EXEC_COMMAND``, piping user_text on stdin."""
    raw = os.environ.get("SAC_A2A_EXEC_COMMAND", "")
    if not raw.strip():
        raise HandlerError(
            "SAC_A2A_EXEC_COMMAND is not set; can't dispatch via 'exec' handler"
        )
    try:
        argv = shlex.split(raw)
    except ValueError as exc:  # stx-allow: fallback (reason: type coercion or format mismatch)
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
    except FileNotFoundError as exc:  # stx-allow: fallback (reason: file may not exist on first use)
        raise HandlerError(f"exec command not found: {argv[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:  # stx-allow: fallback (reason: subprocess execution failure)
        raise HandlerError(f"exec command timeout after {EXEC_TIMEOUT_S:.0f}s") from exc
    if res.returncode != 0:
        raise HandlerError(
            f"exec command failed (rc={res.returncode}): {res.stderr.strip()[:300]}"
        )
    return res.stdout.strip() or "(empty response)"


HANDLERS: dict[str, Callable[[str, str], str]] = {
    "echo": handle_echo,
    "claude_cli": handle_claude_cli,
    "exec": handle_exec,
}
