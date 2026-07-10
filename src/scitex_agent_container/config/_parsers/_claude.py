"""Parser for ``spec.claude``."""

from __future__ import annotations

from .._provider_registry import resolve_provider
from .._provider_types import ProviderSpec
from .._types import ClaudeSpec


def _parse_provider(raw: dict) -> ProviderSpec | None:
    """Parse ``spec.claude.provider`` into a ``ProviderSpec`` or ``None``.

    NAMING COLLISION NOTE (openai-compat-1): this is the NESTED
    ``spec.claude.provider`` — a vendor-agnostic Anthropic-COMPATIBLE
    backend override (DeepSeek, Mimo/Xiaomi, ...) that still runs the
    Claude Agent SDK. It is unrelated to the TOP-LEVEL ``spec.provider``
    (``AgentConfig.provider`` / ``config._provider_types.AgentProvider``),
    which selects WHICH agent SDK family (``anthropic`` vs ``openai``)
    backs the session at all. The top-level field is parsed directly in
    ``_loaders.load_v3`` (mirroring ``spec.runtime``), NOT here — this
    function's scope stays exactly ``spec.claude.provider``.

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
    # PR #319 (lead msg a456b610 2026-06-06): provider.allowed_tools
    # whitelist → ClaudeAgentOptions.tools. Validated to list[str] by
    # _provider_validation.validate_provider; here we coerce defensively
    # so an unvalidated path (tests / fixture configs) still produces a
    # sane shape rather than crashing the runner.
    raw_allowed = block.get("allowed_tools")
    allowed_tools: list[str] = []
    if isinstance(raw_allowed, list):
        allowed_tools = [str(t) for t in raw_allowed if isinstance(t, str) and t]
    return ProviderSpec(
        base_url=str(block.get("base_url", "") or ""),
        auth_token_env=str(block.get("auth_token_env", "") or ""),
        allowed_tools=allowed_tools,
    )


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
