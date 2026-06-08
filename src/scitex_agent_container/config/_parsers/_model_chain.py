"""Parser for ``spec.model.<label>.*`` and the v3 ``spec.claude.*`` alias.

ADR-0018 (operator-co-designed 2026-05-28→29 via Telegram msgs
6816-6831) replaces the Anthropic-centric ``spec.claude.*`` shape
with a label-keyed dict where each label is a complete provider
config, and the dict's insertion order IS the implicit fallback
cascade order. See ``/work/.tmp-adr/ADR-0018.md`` for the full
motivation.

This parser:

* Reads ``spec.model`` (a dict of label → entry) into a
  :class:`config._model_chain_types.ModelChain` preserving operator
  insertion order (= fallback cascade order). Python dicts preserve
  insertion order since 3.7 and PyYAML's ``SafeLoader`` builds
  mappings in source order, so the contract is honored end-to-end
  with no sort or re-key on this side.

* When ``spec.model`` is absent AND ``spec.claude`` is present,
  rewrites the v3 block into a single-label chain under the key
  ``"legacy"`` so the runtime keeps reading a usable shape during
  the v3→v4 transition. Emits a one-time stderr deprecation warning
  per agent at parse time.

* When BOTH ``spec.model`` and ``spec.claude`` are present, returns
  an empty chain — the validator (``_model_chain_validation``)
  surfaces the loud mutual-exclusion error against the raw block.
  Returning empty here keeps the parser non-raising; we do not want
  one operator typo to crash the controller before the validator
  even sees the spec.

The parser is intentionally NON-RAISING for shape errors; the
validator is the single source of "this spec is wrong" diagnostics.
This mirrors :mod:`config._parsers._claude` which silently returns a
``None`` provider for shape errors and lets ``_provider_validation``
emit the named error.

PR A scope: v3 alias path covers the registered-name and bare-string
``spec.claude.provider`` shapes only. The custom-endpoint dict shape
(``{base_url, auth_token_env}``) and the legacy dict shape DO NOT map
cleanly to a single label (the registry-name → backend-metadata
mapping is one-way; reversing it from a custom ``base_url`` would
require a registry round-trip the operator should make explicit).
For those shapes, the alias produces ``provider=""`` so the validator
surfaces a "migrate directly to v4" error pointing at ADR-0018.
"""

from __future__ import annotations

import sys

from .._model_chain_types import ModelChain, ModelLabel
from .._provider_registry import resolve_provider

# One-shot stderr deprecation warning tracker (module-level set).
# Keyed by agent name (None when the loader didn't pass a name in,
# which only happens in direct-parser unit tests). Operators see
# ONE warning per agent per controller process — repeated parses of
# the same spec (e.g. during a config reload) don't re-spam.
#
# Mirrors the "warn once per agent" pattern future modules will adopt;
# kept private so test suites can clear it via the helper below when
# they need to assert the warning fires.
_LEGACY_WARN_EMITTED: set[str | None] = set()


def _emit_v3_alias_warn_once(agent_name: str | None) -> None:
    """Print the v3 alias deprecation warning to stderr at most once.

    Guarded by :data:`_LEGACY_WARN_EMITTED`. Called from the v3→v4
    alias path only — a pure v4 spec is silent at parse time.

    Why stderr instead of ``warnings.warn``: operator runs
    ``sac agent list`` / ``sac agent restart`` interactively, and a
    Python warning would get filtered by their shell or routed to a
    log file they don't tail. Stderr is the channel they see.

    Why one-shot per agent: repeated parses (config reload, dry-run
    + start) would otherwise produce a stutter of identical lines;
    operators ignore stutter, defeating the deprecation push.
    """
    if agent_name in _LEGACY_WARN_EMITTED:
        return
    _LEGACY_WARN_EMITTED.add(agent_name)
    label = agent_name or "<unnamed>"
    sys.stderr.write(
        f"[sac:deprecation] agent '{label}': spec.claude.* is deprecated "
        "— migrate to spec.model.<label>.* (see ADR-0018). v3 specs "
        "continue to load via the spec.model.legacy alias for now.\n"
    )
    sys.stderr.flush()


def _reset_v3_alias_warn_tracker_for_tests() -> None:
    """Test-only helper to clear :data:`_LEGACY_WARN_EMITTED`.

    Exposed so per-test ``capsys`` fixtures observe the warning
    consistently regardless of preceding tests in the session. NOT
    part of the public API; callers outside the test suite should
    never need to reset this.
    """
    _LEGACY_WARN_EMITTED.clear()


def _v3_to_v4_alias(claude_block: dict, agent_name: str | None) -> ModelChain:
    """Rewrite a v3 ``spec.claude.*`` block as a single-label v4 chain.

    The v4 chain has exactly one entry under the key ``"legacy"``
    so the runtime keeps reading a consistent shape during the
    transition. Emits the one-time deprecation warning unconditionally
    — even if the alias produces an empty / malformed label, the
    operator should see the migration nudge.

    Supported v3 ``spec.claude.provider`` shapes (mapped to v4):

    * **string name** (``provider: deepseek``) → ``provider=name``,
      ``model_id=spec.claude.model``.

    * **registered-name dict** (``provider: {name: deepseek}`` — not
      currently a v3 shape but accepted defensively) → same as string.

    * **custom dict** (``{base_url, auth_token_env}`` or
      ``{type: custom, ...}``) → UNSUPPORTED in the v3 alias path.
      Returns a label with ``provider=""`` so the validator surfaces
      a "migrate directly to v4" error pointing at ADR-0018. These
      operators want the env-var-substitution path PR A introduces
      anyway, so making them migrate directly is the right call.

    The :attr:`ModelLabel.account` field passes through verbatim from
    ``spec.claude.account``. v3 had no ``api_key`` field on
    ``spec.claude.*`` (the key was the dict-form provider's
    ``auth_token_env``), so the alias leaves :attr:`ModelLabel.api_key`
    empty — the runtime falls back to the provider registry's
    ``auth_token_env`` exactly as v3 did. Backward-compatible.
    """
    _emit_v3_alias_warn_once(agent_name)
    provider_block = claude_block.get("provider")
    provider_name = ""
    if isinstance(provider_block, str):
        if resolve_provider(provider_block) is not None:
            provider_name = provider_block
        else:
            # Unknown provider name — keep "" so validator surfaces
            # the loud "unknown provider" error via the same code
            # path a v4 spec with a bad provider goes through.
            provider_name = provider_block
    elif isinstance(provider_block, dict):
        # Dict-form provider in v3 was the custom-endpoint or registered-name
        # dict; either way it does NOT map cleanly to a single v4 label.
        # Leave provider_name="" so validator points the operator at
        # ADR-0018 with a clear "migrate directly" message.
        nested_name = provider_block.get("name")
        if isinstance(nested_name, str) and resolve_provider(nested_name) is not None:
            provider_name = nested_name
        # else: leave "" — custom dict shape can't be reduced.
    # ``None`` / absent provider block → leave provider_name="";
    # validator surfaces the required-field error.

    model_id = str(claude_block.get("model", "") or "")
    account = str(claude_block.get("account", "") or "")
    return {
        "legacy": ModelLabel(
            provider=provider_name,
            model_id=model_id,
            account=account,
            api_key="",
        )
    }


def parse_model_chain(spec: dict, agent_name: str | None = None) -> ModelChain:
    """Parse ``spec.model`` into a :data:`ModelChain` (ADR-0018 PR A).

    Behaviour matrix:

    * ``spec.model`` present, ``spec.claude`` absent → parse v4
      directly. Operator insertion order is preserved.

    * ``spec.model`` absent, ``spec.claude`` present → rewrite via
      :func:`_v3_to_v4_alias`. Emits the one-time deprecation
      warning. Returns a single-label chain under ``"legacy"``.

    * Both present → return empty dict. The validator surfaces the
      loud mutual-exclusion error against the raw block. We do NOT
      raise here; the parser stays non-raising.

    * Neither present → return empty dict (no chain configured;
      validator treats absence as "model chain optional in v4 too if
      spec.claude provides one" — see
      :func:`config._model_chain_validation.validate_model_chain`).

    The parser is defensive against per-label non-dict entries: a
    ``None`` / scalar value for a label produces a default
    :class:`ModelLabel` (all fields empty), letting the validator
    surface a structured "field X is required" error per label rather
    than a parser KeyError.
    """
    raw_model = spec.get("model")
    raw_claude = spec.get("claude")

    has_model = isinstance(raw_model, dict) and raw_model
    has_claude = isinstance(raw_claude, dict) and raw_claude

    if has_model and has_claude:
        # Mutex; defer to validator. Empty dict means "no usable chain".
        return {}

    if not has_model and has_claude:
        return _v3_to_v4_alias(raw_claude, agent_name)

    if not has_model:
        # Neither v4 chain nor v3 alias — empty chain (legitimate
        # "no model configured" case; loader will still produce an
        # AgentConfig with defaults).
        return {}

    # has_model: parse v4 chain in insertion order.
    chain: ModelChain = {}
    for label_name, label_entry in raw_model.items():
        # Defensive: a per-label non-dict (yaml typo, accidental scalar)
        # produces an all-empty ModelLabel rather than a parser raise.
        # Validator surfaces the per-field "required" errors.
        if not isinstance(label_entry, dict):
            chain[str(label_name)] = ModelLabel()
            continue
        provider = str(label_entry.get("provider", "") or "")
        model_id = str(label_entry.get("model_id", "") or "")
        account = str(label_entry.get("account", "") or "")
        api_key = str(label_entry.get("api_key", "") or "")
        chain[str(label_name)] = ModelLabel(
            provider=provider,
            model_id=model_id,
            account=account,
            api_key=api_key,
        )
    return chain


__all__ = [
    "parse_model_chain",
    "_v3_to_v4_alias",
    "_emit_v3_alias_warn_once",
    "_reset_v3_alias_warn_tracker_for_tests",
]
