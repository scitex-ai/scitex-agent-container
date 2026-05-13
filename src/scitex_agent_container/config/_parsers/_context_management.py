"""Parser for ``spec.context_management``."""

from __future__ import annotations

from .._types import ContextManagementConfig


def parse_context_management(spec: dict) -> ContextManagementConfig:
    raw = spec.get("context_management", {}) or {}
    try:
        trigger = float(raw.get("trigger_at_percent", 70.0))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        trigger = 70.0
    strategy = str(raw.get("strategy", "noop") or "noop")
    if strategy not in ("compact", "restart", "noop"):
        strategy = "noop"
    try:
        warn_n = int(raw.get("warn_before_n_checks", 0))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        warn_n = 0
    try:
        interval = int(raw.get("check_interval_seconds", 300))
    except (
        TypeError,
        ValueError,
    ):  # stx-allow: fallback (reason: type coercion or format mismatch)
        interval = 300
    state_file = str(
        raw.get("state_file", "~/.scitex/agent-container/state/<agent>.json")
    )
    return ContextManagementConfig(
        trigger_at_percent=trigger,
        strategy=strategy,
        warn_before_n_checks=max(0, warn_n),
        check_interval_seconds=max(1, interval),
        state_file=state_file,
    )
