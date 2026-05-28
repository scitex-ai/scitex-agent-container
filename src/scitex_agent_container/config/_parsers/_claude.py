"""Parser for ``spec.claude``."""

from __future__ import annotations

from .._provider_registry import resolve_provider
from .._provider_types import ProviderSpec
from .._types import ClaudeSpec


def _parse_provider(raw: dict) -> ProviderSpec | None:
    """Parse ``spec.claude.provider`` into a ``ProviderSpec`` or ``None``.

    Accepts two shapes (back-compat — see ADR-0011 extension):

    * **string** (new shape, operator directive 2026-05-28 msg 6783):
      a registered provider name, e.g. ``provider: mimo``. Resolved
      against :mod:`config._provider_registry` to recover the backend
      metadata. An ``"anthropic"`` string-form (or any registered name
      whose registry entry has ``base_url=None``) parses to ``None`` —
      it means "default Anthropic backend, no override".
    * **dict** (existing shape): ``{base_url, auth_token_env}``.
      Forwarded verbatim to :class:`ProviderSpec`; the validator
      enforces both fields are non-empty.
    * anything else (absent, explicit-null, list, ...) → ``None``.

    Unknown string names are silently surfaced as ``None`` here so
    the validator (which runs against the raw block) is the single
    source of the "unknown provider" diagnostic — keeps the error
    message + the known-providers list in one place.
    """
    block = raw.get("provider")
    if isinstance(block, str):
        entry = resolve_provider(block)
        if entry is None:
            return None
        base_url = entry.get("base_url") or ""
        auth_token_env = entry.get("auth_token_env") or ""
        if not base_url and not auth_token_env:
            # Registered "no override" sentinel (e.g. "anthropic").
            return None
        return ProviderSpec(base_url=base_url, auth_token_env=auth_token_env)
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
