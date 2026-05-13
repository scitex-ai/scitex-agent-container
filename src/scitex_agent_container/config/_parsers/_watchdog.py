"""Parser for ``spec.watchdog``."""

from __future__ import annotations

from .._types import WatchdogSpec


def parse_watchdog(spec: dict) -> WatchdogSpec:
    raw = spec.get("watchdog", {}) or {}
    responses = raw.get("responses", {}) or {}
    return WatchdogSpec(
        enabled=raw.get("enabled", False),
        interval=float(raw.get("interval", 1.5)),
        resp_y_n=str(responses.get("y_n", "1")),
        resp_y_y_n=str(responses.get("y_y_n", "2")),
        resp_waiting=str(responses.get("waiting", "/speak-and-call")),
    )
