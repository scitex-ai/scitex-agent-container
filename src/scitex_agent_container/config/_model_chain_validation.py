"""Validation for ``spec.model.<label>.*`` (ADR-0018 v4 PR A).

Single entry point: :func:`validate_model_chain`. Returns a list of
human-readable error strings (empty list = clean). Mirrors
:mod:`config._provider_validation`'s shape so the top-level
:mod:`config._validation` can extend its existing error list with our
output.

Error strings are operator-targeted: every error names the offending
label, the offending field, and (when applicable) the closed set of
valid values. The known-providers list is emitted via
:func:`config._provider_registry.list_providers` so the diagnostic
stays in sync with whatever entries the registry actually carries.

Field semantics enforced (per ADR-0018 §"Key naming"):

* ``provider`` REQUIRED, non-empty string, must resolve via
  :mod:`_provider_registry`.
* ``model_id`` REQUIRED, non-empty string.
* ``account`` and ``api_key`` MUTUALLY EXCLUSIVE within a single
  label.
* ``account`` declared on a NON-Anthropic provider → soft warning
  (operator may have copy-pasted from a v3 spec; not a hard fail).
* ``api_key`` literal (not a ``$VAR`` / ``${VAR}`` ref) → soft
  warning ("secrets in spec.yaml is anti-pattern").

PR A does NOT resolve env-var references; the parsed value is the
RAW string. PR B does the at-start resolution. Splitting recognition
out keeps the validator host-independent (the controller validates
specs that will run on remote hosts whose env vars it cannot read).

The "is this an Anthropic provider" check is recovered from the
registry: a registered entry with ``auth_token_env == None`` is the
"default Anthropic OAuth backend" sentinel. Any other entry is a
non-Anthropic provider that uses an API key, not OAuth. We do NOT
hard-code "anthropic" in name comparisons — the registry IS the
truth.
"""

from __future__ import annotations

from ._env_var_substitution import is_env_var_ref
from ._provider_registry import list_providers, resolve_provider


def _provider_uses_oauth(provider_name: str) -> bool:
    """Return ``True`` when ``provider_name`` is the Anthropic-OAuth shape.

    Recovered from the registry: an entry with ``auth_token_env ==
    None`` (the "default Anthropic OAuth backend" sentinel)
    represents the OAuth-based Anthropic provider. Every other
    registered entry has an ``auth_token_env`` and uses an API key.

    Unknown provider names return ``False`` — the validator already
    surfaces an "unknown provider" error on the same label, and we
    do not want to ALSO claim the unknown name "may have copy-paste
    issues" (would distract from the actionable error).
    """
    entry = resolve_provider(provider_name)
    if entry is None:
        return False
    return entry.get("auth_token_env") is None


def _validate_label(
    label_name: str, label_entry: object, *, agent_name: str | None
) -> list[str]:
    """Validate one ``spec.model.<label>`` entry. Returns error strings.

    Per-label errors are prefixed with ``spec.model.<label>.<field>``
    so the operator sees exactly which line of yaml to fix.
    """
    errors: list[str] = []
    if not isinstance(label_entry, dict):
        errors.append(
            f"spec.model.{label_name} must be a mapping with provider/model_id/"
            f"(account|api_key) keys; got {type(label_entry).__name__}"
        )
        return errors

    provider = label_entry.get("provider")
    if not isinstance(provider, str) or not provider:
        errors.append(
            f"spec.model.{label_name}.provider is required and must be a "
            "non-empty string naming a registered provider "
            f"(known: {', '.join(list_providers())})."
        )
    else:
        if resolve_provider(provider) is None:
            errors.append(
                f"spec.model.{label_name}.provider='{provider}' is not a "
                f"registered provider name. Known providers: "
                f"{', '.join(list_providers())}. To add a new backend, "
                "append it to PROVIDERS in "
                "scitex_agent_container/config/_provider_registry.py."
            )

    model_id = label_entry.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        errors.append(
            f"spec.model.{label_name}.model_id is required and must be a "
            "non-empty string (provider-specific model identifier — "
            "e.g. 'claude-sonnet-4-6' for Anthropic, 'mimo-v2.5-pro' "
            "for Xiaomi)."
        )

    account_val = label_entry.get("account")
    api_key_val = label_entry.get("api_key")
    has_account = isinstance(account_val, str) and account_val
    has_api_key = isinstance(api_key_val, str) and api_key_val

    if has_account and has_api_key:
        errors.append(
            f"spec.model.{label_name}.account and "
            f"spec.model.{label_name}.api_key are mutually exclusive — "
            "an API-key backend needs no OAuth, and an OAuth backend uses "
            "stored credentials, not a key. Set exactly one."
        )

    # Soft warnings — non-fatal but surfaced. Prefixed with [warn] so a
    # caller can split them out from the fatal errors. PR A's validator
    # caller (top-level `_validation.validate_raw`) treats every entry
    # as an error; the [warn] prefix preserves the signal for the
    # follow-up split (PR B / PR C observability).
    if has_account and isinstance(provider, str) and provider:
        if not _provider_uses_oauth(provider):
            errors.append(
                f"[warn] spec.model.{label_name}.account is set on a "
                f"non-Anthropic provider ('{provider}'); the account "
                "field selects an Anthropic OAuth Max account and has no "
                "effect for API-key backends. Remove the field or move "
                "to api_key."
            )

    if has_api_key and not is_env_var_ref(str(api_key_val)):
        errors.append(
            f"[warn] spec.model.{label_name}.api_key is a literal value; "
            "secrets in spec.yaml is anti-pattern (spec.yaml syncs via "
            "dotfiles git). Use the $VAR or ${VAR} form to reference an "
            "env var instead."
        )

    return errors


def validate_model_chain(
    model_block: object,
    *,
    agent_name: str | None = None,
) -> list[str]:
    """Validate the raw ``spec.model`` block. Returns error strings.

    ``model_block`` is the raw yaml value (``spec.get("model")``) —
    not the parsed :class:`ModelChain`. We validate against the raw
    block so the parser can stay non-raising; the validator is the
    single source of "this spec is wrong" diagnostics.

    ``None`` / absent (key not in spec) → empty list. v4 is OPTIONAL
    if the v3 ``spec.claude.*`` alias provides the chain — the
    top-level :mod:`_validation` enforces the "at least one chain
    source" rule across both keys.

    Empty dict ``{}`` → error. An explicit empty block is operator
    intent that we cannot interpret; better to surface "declare at
    least one label" loudly than silently fall back.

    Non-dict → error naming the type.

    ``agent_name`` is currently unused (no per-agent error
    customization yet) — kept in the signature for symmetry with
    :func:`config._parsers._model_chain.parse_model_chain` and to
    enable future per-agent warnings (PR B / PR C).
    """
    if model_block is None:
        return []

    if not isinstance(model_block, dict):
        return [
            "spec.model must be a dict of label → config (label name = "
            "operator-chosen string; config = {provider, model_id, "
            f"account|api_key}}); got {type(model_block).__name__}"
        ]

    if not model_block:
        return [
            "spec.model is empty; declare at least one label entry. "
            "Even single-config specs require a label "
            "(e.g. 'default:'). See ADR-0018 §'Single-config case'."
        ]

    errors: list[str] = []
    for label_name, label_entry in model_block.items():
        errors.extend(
            _validate_label(str(label_name), label_entry, agent_name=agent_name)
        )
    return errors


__all__ = ["validate_model_chain"]
