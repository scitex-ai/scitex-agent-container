"""Parser for ``spec.claude``."""

from __future__ import annotations

from .._types import ClaudeSpec


def parse_claude(spec: dict) -> ClaudeSpec:
    raw = spec.get("claude", {}) or {}
    # Top-level `session:` takes precedence over `claude.session` for
    # ergonomics (it's the primary knob agents care about). Falls back to
    # the nested field for backward compat, then the default.
    session = spec.get("session")
    if session is None:
        session = raw.get("session", "continue-or-new")
    continue_max_age = raw.get("continue_max_age_minutes")
    if continue_max_age is not None:
        try:
            continue_max_age = int(continue_max_age)
        except (
            TypeError,
            ValueError,
        ):  # stx-allow: fallback (reason: type coercion or format mismatch)
            continue_max_age = None
    raw_options = raw.get("raw_options", {}) or {}
    if not isinstance(raw_options, dict):
        raw_options = {}
    return ClaudeSpec(
        model=str(raw.get("model", "") or ""),
        channels=raw.get("channels", []) or [],
        flags=raw.get("flags", []) or [],
        session=session,
        continue_max_age_minutes=continue_max_age,
        resume_id=str(raw.get("resume_id", "") or ""),
        auto_accept=raw.get("auto_accept", True),
        raw_options=dict(raw_options),
    )
