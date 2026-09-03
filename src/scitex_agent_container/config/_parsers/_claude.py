"""Parser for ``spec.claude``."""

from __future__ import annotations

from .._provider_parse import parse_provider_value
from .._types import ClaudeSpec


def _parse_provider(raw: dict):
    """Parse ``spec.claude.provider`` into a ``ProviderSpec`` or ``None``.

    This is the NESTED ``spec.claude.provider`` — a vendor-agnostic
    Anthropic-COMPATIBLE backend override (DeepSeek, Mimo/Xiaomi, ...)
    that still runs the Claude Agent SDK. It is unrelated to the
    TOP-LEVEL ``spec.harness`` (``AgentConfig.harness`` /
    ``config._harness_types``), which selects WHICH agent SDK runs the
    session at all — that key used to be spelled ``spec.provider``,
    which is what made this note necessary. The top-level field is
    resolved in ``_loaders.load_v3`` (mirroring ``spec.runtime``), NOT
    here — this function's scope stays exactly ``spec.claude.provider``.

    The FOLD itself lives in ``config._provider_parse.
    parse_provider_value`` and is shared with ``spec.engines.<key>.
    provider`` (``config._engine_types``), so the multi-backend surface
    accepts exactly the vocabulary this one does — see that module for
    the accepted shapes and for why an unknown string name folds to
    ``None`` (the validator owns that diagnostic).
    """
    return parse_provider_value(raw.get("provider"))


def parse_claude(spec: dict) -> ClaudeSpec:
    raw = spec.get("claude", {}) or {}
    # Top-level `session:` takes precedence over `claude.session` for
    # ergonomics (it's the primary knob agents care about). Falls back to
    # the nested field for backward compat, then the default.
    #
    # Default flipped to ``fresh`` (2026-06-22, "fresh by default, opt-in
    # continue"): a spec that OMITS ``session`` starts an independent
    # session — the right behaviour for experiment trials. Coordinator /
    # long-lived roles that omit the field are mapped back to ``continue``
    # in ``_loaders.py`` (it has the ``metadata.labels.role`` + ``spec.env``
    # role that this parser cannot see), so the flip does not silently
    # break long-lived agents. An EXPLICIT ``session: fresh`` on a
    # coordinator stays fresh (the loader only applies the role-default
    # when the field was omitted entirely).
    session = spec.get("session")
    if session is None:
        session = raw.get("session", "fresh")
    # Alias normalization. ``fresh`` is the canonical "always start a new
    # session" value; the pre-2026-06 names ``new-session`` / ``new`` are
    # accepted aliases for it (back-compat for existing specs / the SDK
    # path). ``continue-or-new`` is the safe-fallback alias for
    # ``continue``.
    _SESSION_ALIASES = {
        "continue-or-new": "continue",
        "new": "fresh",
        "new-session": "fresh",
    }
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
    # Account POOL — ``credentials_files`` (plural). A list of host paths
    # to ``.credentials.json`` files; the start pre-flight picks one
    # quota-aware. Coerce defensively to ``list[str]`` (the validator is
    # the SSOT for the "must be a list of strings" diagnostic); a non-list
    # / non-string entries degrade to an empty pool here so an unvalidated
    # fixture path never crashes the loader.
    raw_cred_files = raw.get("credentials_files", []) or []
    credentials_files: list[str] = []
    if isinstance(raw_cred_files, list):
        credentials_files = [
            str(p) for p in raw_cred_files if isinstance(p, str) and p.strip()
        ]
    return ClaudeSpec(
        model=str(raw.get("model", "") or ""),
        channels=raw.get("channels", []) or [],
        flags=raw.get("flags", []) or [],
        session=session,
        continue_max_age_minutes=continue_max_age,
        resume_id=str(raw.get("resume_id", "") or ""),
        auto_accept=raw.get("auto_accept", True),
        account=str(raw.get("account", "") or ""),
        credentials_file=str(raw.get("credentials_file", "") or ""),
        credentials_files=credentials_files,
        provider=_parse_provider(raw),
        raw_options=dict(raw_options),
    )
