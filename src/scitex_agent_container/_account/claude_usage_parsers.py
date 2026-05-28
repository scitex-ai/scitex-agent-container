"""Pure payload-shape parsers for the Anthropic usage API.

Split out of ``claude_usage`` so the orchestrator (token read / refresh /
cache / fetch) stays focused on I/O and side effects, while the
shape-dispatch + per-window extraction lives here as deterministic
pure-function code.

Schemas recognised
------------------
1. **New (2026-05-28+) Anthropic schema** — a dict carrying per-window
   sub-dicts under ``five_hour`` / ``seven_day`` keys, each with
   ``utilization`` (0-100 float %) and ``resets_at`` (ISO-8601 string).
   Raw token counts are not exposed by this schema, so
   ``used_tokens_*`` / ``limit_tokens_*`` stay ``None``.
2. **Legacy wrapped dict** — ``{"windows": [...]}`` or
   ``{"data": [...]}`` containing window objects with ``window`` /
   ``used`` / ``limit`` / ``resetAt`` keys.
3. **Legacy single-window dict** — ``{"window": "5h", ...}``; wrapped
   and re-routed through the legacy list parser.
4. **Legacy list** — ``[{"window": "5h", ...}, ...]`` directly.

Anything else returns ``None`` and is logged at WARNING with a key
summary so the operator has something to grep for.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

# Anthropic's 2026-05-28+ usage API uses snake_case per-window dicts that
# carry ``utilization`` (0-100 float percentage) + ``resets_at``
# (ISO-8601). Raw token counts are no longer exposed by the API, so
# used_tokens_* / limit_tokens_* stay None when parsed from this shape.
_NEW_SHAPE_KEYS: dict[str, str] = {
    "five_hour": "5h",
    "seven_day": "7d",
}


def _extract_quota_from_payload(payload: Any) -> dict[str, Any] | None:
    """Shape-dispatch ``payload`` and extract per-window quota fields.

    Returns ``None`` for any unrecognised shape (logged at WARNING with
    the payload's top-level key summary).
    """
    if isinstance(payload, dict):
        if any(k in payload for k in _NEW_SHAPE_KEYS):
            return _parse_new_shape(payload)
        wrapped = payload.get("windows")
        if isinstance(wrapped, list):
            return _parse_windows(wrapped)
        wrapped = payload.get("data")
        if isinstance(wrapped, list):
            return _parse_windows(wrapped)
        if "window" in payload:
            return _parse_windows([payload])
        _logger.warning(
            "claude_usage: unrecognised dict payload; keys=%r",
            sorted(payload.keys()),
        )
        return None
    if isinstance(payload, list):
        return _parse_windows(payload)
    _logger.warning(
        "claude_usage: payload was %s, not dict/list",
        type(payload).__name__,
    )
    return None


def _parse_new_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract used_pct_* / reset_at_* from the 2026-05-28+ schema.

    Token counts are not surfaced by this schema; the corresponding
    fields are emitted as ``None`` so downstream consumers that only
    look at percentages (``quota_watch``, ``_account_status``,
    ``account_group``) keep working unchanged.
    """
    result: dict[str, Any] = {}
    for src_key, suffix in _NEW_SHAPE_KEYS.items():
        window = payload.get(src_key) if isinstance(payload, dict) else None
        if isinstance(window, dict):
            util = window.get("utilization")
            reset = window.get("resets_at")
            result[f"used_pct_{suffix}"] = (
                float(util) if isinstance(util, (int, float)) else None
            )
            result[f"reset_at_{suffix}"] = reset if isinstance(reset, str) else None
        else:
            result[f"used_pct_{suffix}"] = None
            result[f"reset_at_{suffix}"] = None
        # 2026-05-28+ schema does not surface raw token counts.
        result[f"used_tokens_{suffix}"] = None
        result[f"limit_tokens_{suffix}"] = None
    return result


def _parse_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract 5h and 7d quota values from the legacy API window list."""
    result: dict[str, Any] = {}
    for w in windows:
        if not isinstance(w, dict):
            continue
        win = w.get("window")
        used = w.get("used")
        limit = w.get("limit")
        reset_at = w.get("resetAt")
        if win not in ("5h", "7d"):
            continue
        suffix = win  # already "5h" or "7d"
        result[f"used_tokens_{suffix}"] = used if isinstance(used, int) else None
        result[f"limit_tokens_{suffix}"] = limit if isinstance(limit, int) else None
        if isinstance(used, int) and isinstance(limit, int) and limit > 0:
            result[f"used_pct_{suffix}"] = round(used / limit * 100, 2)
        else:
            result[f"used_pct_{suffix}"] = None
        result[f"reset_at_{suffix}"] = reset_at if isinstance(reset_at, str) else None
    return result
