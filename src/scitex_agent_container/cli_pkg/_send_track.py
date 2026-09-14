"""The backgroundable command that reaches an agent's native HTTP boundary.

There is one prompt transport regardless of harness: ``sac agents send`` posts
to ``/v1/turn``. Asynchronous adapters answer with a responder-issued
``scitex_dev.status.StatusCode(kind="http", code=202, ...)`` plus an exchange
id; the command returns that non-final receipt immediately. Callers can poll
``/v1/exchanges/<id>`` when they require final delivery evidence.
Hermes implements the receiving edge with its native ``prompt.submit`` RPC.

The retired implementation inspected tmux state and switched TUI agents to
``sac agents deliver``. That verb pasted text and Enter into a terminal and was
therefore sensitive to composer timing. A background tracking command must not
reintroduce that transport indirectly: every command built here names the same
native HTTP verb.

ONE SOURCE, TWO RENDERINGS. The dispatch payload carries both a shell string
(``track_command``) and an argv list (``track_command_argv``). Those were built
independently from two separate literals, so a change to either could silently
diverge from the other. Here the string is derived from the argv, which makes
divergence unrepresentable rather than merely unlikely.
"""

from __future__ import annotations

import shlex

__all__ = [
    "build_track_command",
    "build_track_command_argv",
]


def build_track_command_argv(name: str, prompt: str) -> list[str]:
    """The argv the caller should run to deliver ``prompt`` to ``name``."""
    return ["sac", "agents", "send", name, prompt]


def build_track_command(name: str, prompt: str) -> str:
    """Shell rendering of :func:`build_track_command_argv`.

    Derived from the argv rather than formatted separately, so the two cannot
    disagree about the verb.
    """
    return shlex.join(build_track_command_argv(name, prompt))


# EOF
