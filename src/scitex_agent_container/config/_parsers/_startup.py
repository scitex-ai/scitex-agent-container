"""Parsers for ``spec.startup_commands`` / ``spec.startup_prompts``.

v3 ``startup_prompts`` is the claude SDK mission. ``startup_commands``
is a list of shell commands executed inside the container BEFORE the
claude SDK starts (see ``runtimes._apptainer_inner_argv``).
"""

from __future__ import annotations

from .._types import StartupCommand


def parse_startup_commands(spec: dict) -> list[StartupCommand]:
    raw = spec.get("startup_commands", []) or []
    return [
        StartupCommand(
            delay=int(item.get("delay", 0)),
            command=item.get("command", ""),
        )
        for item in raw
        if isinstance(item, dict) and item.get("command")
    ]
