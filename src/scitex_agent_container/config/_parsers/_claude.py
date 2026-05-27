"""Parser for ``spec.claude``."""

from __future__ import annotations

from .._provider_types import ProviderSpec
from .._types import ClaudeSpec


def _parse_provider(raw: dict) -> ProviderSpec | None:
    """Parse ``spec.claude.provider`` into a ``ProviderSpec`` or ``None``.

    Absent / explicit-null block → ``None`` (default Anthropic backend).
    Non-empty fields are surfaced verbatim; the validator enforces that
    ``base_url`` + ``auth_token_env`` are non-empty when the block exists.
    """
    block = raw.get("provider")
    if not isinstance(block, dict):
        return None
    return ProviderSpec(
        base_url=str(block.get("base_url", "") or ""),
        auth_token_env=str(block.get("auth_token_env", "") or ""),
    )


def parse_claude(spec: dict) -> ClaudeSpec:
    raw = spec.get("claude", {}) or {}
    # Top-level `session:` takes precedence over `claude.session` for
    # ergonomics (it's the primary knob agents care about). Falls back to
    # the nested field for backward compat, then the default.
    session = spec.get("session")
    if session is None:
        session = raw.get("session", "continue")
    # Legacy alias normalization (REQUIREMENT_SUMMARY §3 #6):
    #   continue-or-new -> continue   (same semantics: safe-fallback)
    #   new             -> new-session (renamed for clarity)
    _SESSION_ALIASES = {"continue-or-new": "continue", "new": "new-session"}
    session = _SESSION_ALIASES.get(str(session), session)
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
        account=str(raw.get("account", "") or ""),
        provider=_parse_provider(raw),
        raw_options=dict(raw_options),
    )
