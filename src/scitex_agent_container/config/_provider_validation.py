"""Validation for ``spec.claude.provider`` — string form + dict form.

Extracted from :mod:`config._validation` to keep that file under the
project's 512-line cap and to keep the three provider-axis modules
(:mod:`_provider_registry`, :mod:`_provider_types`,
:mod:`_provider_validation`) cohesive.

ADR-0011 extension (operator directive 2026-05-28 msg 6783):
``spec.claude.provider`` now accepts a registered string identifier
(``provider: mimo``) in addition to the existing
``{base_url, auth_token_env}`` dict form. The string form is resolved
through :mod:`_provider_registry`; unknown names surface a loud error
listing the registered providers.

The dict form rule is unchanged: an incomplete dict override would
silently fall back to Anthropic at runtime, which we refuse to allow.
"""

from __future__ import annotations


def validate_provider(provider_block: object) -> list[str]:
    """Validate ``spec.claude.provider`` (vendor backend override).

    Accepts:

    * **string** — a registered provider name (see
      :mod:`config._provider_registry`). Unknown names surface a loud
      error listing the registered providers.
    * **dict** ``{name, auth_token}`` — registered provider plus declarative
      auth. ``auth_token: auto`` resolves/generates the secret at launch.
    * **dict** ``{base_url, auth_token_env}`` — legacy explicit endpoint shape.
    * anything else (absent, explicit-null) → no errors (provider
      feature unused).
    """
    if isinstance(provider_block, str):
        from ._provider_registry import list_providers, resolve_provider

        if resolve_provider(provider_block) is None:
            known = ", ".join(list_providers())
            return [
                f"spec.claude.provider='{provider_block}' is not a registered "
                f"provider name. Known providers: {known}. To add a new "
                "backend, append it to PROVIDERS in "
                "scitex_agent_container/config/_provider_registry.py."
            ]
        return []
    if not isinstance(provider_block, dict):
        return []
    errors: list[str] = []
    name = provider_block.get("name")
    if name is not None:
        from ._provider_registry import list_providers, resolve_provider

        if not isinstance(name, str) or not name:
            errors.append(
                "spec.claude.provider.name must be a non-empty string when set."
            )
        elif resolve_provider(name) is None:
            errors.append(
                f"spec.claude.provider.name='{name}' is not registered. "
                f"Known providers: {', '.join(list_providers())}."
            )
    else:
        for field_name in ("base_url", "auth_token_env"):
            val = provider_block.get(field_name)
            if val is None or val == "":
                errors.append(
                    f"spec.claude.provider.{field_name} is required and must be "
                    "non-empty when spec.claude.provider.name is omitted."
                )
            elif not isinstance(val, str):
                errors.append(
                    f"spec.claude.provider.{field_name} must be a string, got "
                    f"{type(val).__name__}"
                )
    if "auth_token" in provider_block:
        auth_token = provider_block.get("auth_token")
        if not isinstance(auth_token, str) or not auth_token:
            errors.append(
                "spec.claude.provider.auth_token must be a non-empty string "
                "when set; use 'auto' for launch-time resolution."
            )
    # PR #319 (lead msg a456b610 2026-06-06): optional allowed_tools
    # whitelist. If present it MUST be a list of non-empty strings;
    # anything else is a loud config error (silent type-coercion would
    # let a yaml string-not-list propagate into ClaudeAgentOptions.tools
    # and downstream the CLI / SDK would fail in less actionable ways).
    if "allowed_tools" in provider_block:
        raw = provider_block.get("allowed_tools")
        if not isinstance(raw, list):
            errors.append(
                "spec.claude.provider.allowed_tools must be a list of "
                f"strings, got {type(raw).__name__}"
            )
        else:
            for idx, item in enumerate(raw):
                if not isinstance(item, str) or not item:
                    errors.append(
                        f"spec.claude.provider.allowed_tools[{idx}] must be a "
                        f"non-empty string, got {item!r}"
                    )
    return errors


def provider_is_active(provider_block: object) -> bool:
    """Return ``True`` when ``provider_block`` declares an active backend
    override.

    Used by the validator's mutual-exclusion check against
    ``spec.claude.account``. Tracks the same "active" semantics as the
    runtime side: a registered string-form name with a non-null
    ``base_url`` in the registry, OR a dict with both fields set.

    A registered name whose registry entry has ``base_url=None`` (e.g.
    ``"anthropic"`` — the "use the default OAuth backend" sentinel) is
    NOT active; it is parser-equivalent to ``provider: ~``.
    """
    if isinstance(provider_block, str):
        from ._provider_registry import resolve_provider

        entry = resolve_provider(provider_block)
        if entry is None:
            return False
        return bool(entry.get("base_url"))
    if not isinstance(provider_block, dict):
        return False
    name = provider_block.get("name")
    if isinstance(name, str):
        from ._provider_registry import resolve_provider

        entry = resolve_provider(name)
        return bool(entry and entry.get("base_url"))
    return True


__all__ = ["validate_provider", "provider_is_active"]
