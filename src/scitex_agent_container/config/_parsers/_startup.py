"""Parsers for ``spec.startup`` / ``spec.startup_commands`` / ``spec.startup_prompts``.

v3 ``startup_prompts`` field lives here alongside the legacy
``startup_commands`` and the newer ``startup`` block (commands +
ready-pattern gating).
"""

from __future__ import annotations

from .._types import ReadyPattern, StartupCommand, StartupSpec
from ._helpers import _parse_command_list


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


def parse_startup(spec: dict) -> StartupSpec:
    """Parse the opt-in ``spec.startup`` block (todo#291).

    Missing or malformed → empty ``StartupSpec`` (legacy behavior). When
    ``spec.startup.commands`` is absent we shadow the legacy top-level
    ``spec.startup_commands`` so an operator can add a ready gate without
    moving their existing command list.
    """
    raw = spec.get("startup")
    if not isinstance(raw, dict):
        legacy = parse_startup_commands(spec)
        return StartupSpec(commands=legacy)

    patterns_raw = raw.get("ready_patterns", []) or []
    patterns: list[ReadyPattern] = []
    for item in patterns_raw:
        if isinstance(item, str):
            patterns.append(ReadyPattern(regex=item))
        elif isinstance(item, dict) and item.get("regex"):
            patterns.append(ReadyPattern(regex=str(item["regex"])))

    try:
        idle_ticks = max(1, int(raw.get("ready_idle_ticks", 3)))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        idle_ticks = 3
    try:
        poll_interval = max(0.05, float(raw.get("ready_poll_interval_seconds", 0.5)))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        poll_interval = 0.5
    try:
        timeout = max(1.0, float(raw.get("ready_timeout_seconds", 60.0)))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        timeout = 60.0

    on_timeout = str(
        raw.get("on_timeout", "capture_and_proceed") or "capture_and_proceed"
    )
    if on_timeout not in ("capture_and_fail", "capture_and_proceed"):
        on_timeout = "capture_and_proceed"

    commands = _parse_command_list(raw.get("commands"))
    if not commands:
        commands = parse_startup_commands(spec)

    return StartupSpec(
        ready_patterns=patterns,
        ready_idle_ticks=idle_ticks,
        ready_poll_interval_seconds=poll_interval,
        ready_timeout_seconds=timeout,
        on_timeout=on_timeout,
        commands=commands,
    )
