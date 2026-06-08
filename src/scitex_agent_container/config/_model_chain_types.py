"""ModelLabel / ModelChain dataclasses for ``spec.model.<label>.*`` (ADR-0018 v4).

ADR-0018 (operator-co-designed 2026-05-28→29 via Telegram msgs
6816-6831) replaces the Anthropic-centric ``spec.claude.*`` shape
(PR #244, ADR-0011, ADR-0016) with a label-keyed dict where each label
declares a COMPLETE provider config, and the dict's insertion order
IS the implicit fallback cascade order. See
``/work/.tmp-adr/ADR-0018.md`` for the full motivation.

This module owns the parsed in-memory shape. It is intentionally tiny
and side-effect-free so it can be imported from both parser and
validator without a circular dependency on
:mod:`config._provider_registry` (registry resolution stays in the
validator). The dataclass mirrors :mod:`config._provider_types`'s tone
on purpose — operators read both side-by-side.

Lives in its own module to keep ``_types.py`` under the project's
512-line cap (mirrors ``_proxy_types.py`` /
``_provider_types.py``). Re-exported from
:mod:`scitex_agent_container.config` alongside the rest of the spec
dataclasses.

PR A scope (this file): schema-only — no runtime fallback dispatch.
The runtime continues to read ``spec.claude.*`` via the v3-to-v4 alias
populated in :mod:`config._parsers._model_chain`. Runtime fallback
dispatch lands in PR B (ADR-0018 §"Implementation outline").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelLabel:
    """Parsed shape of one ``spec.model.<label>.*`` entry (ADR-0018 v4).

    Each label is a COMPLETE provider configuration — provider name,
    model id, and exactly one auth surface (Anthropic OAuth ``account``
    XOR generic ``api_key``). The label key (the dict key under
    ``spec.model``) is operator-chosen — semantic (``primary`` /
    ``backup-xiaomi``) or neutral (``label-1`` / ``label-2``) both
    fine; ``<label-N>`` is the canonical example form for
    documentation. The label key is NOT stored on the instance because
    it IS the dict key of the surrounding :data:`ModelChain` mapping;
    keeping it implicit avoids the dict-key-vs-attribute drift bug
    class.

    Field semantics (per ADR-0018 §"Key naming"):

    * :attr:`provider` — REQUIRED. Registered provider name in
      :data:`config._provider_registry.PROVIDERS` (the existing PR
      #244 registry). Unknown names surface a loud validator error
      with the known-providers list. PR A does NOT extend the
      registry.

    * :attr:`model_id` — REQUIRED. Non-empty string. Renamed from
      ``model`` (v3 spec.claude.model) so the field name doesn't
      collide with the outer ``spec.model`` dict key. The provider
      decides the accepted form (``claude-sonnet-4-6`` for
      Anthropic, ``mimo-v2.5-pro`` for Xiaomi, etc.); v4 does NOT
      apply the v3 ``claude-*`` regex here.

    * :attr:`account` — OPTIONAL. Used only for Anthropic-OAuth
      providers (operator's stored Max account selector — see
      :class:`config._types.ClaudeSpec.account` for the snapshot
      semantics). For non-Anthropic providers, declaring it is a
      validation WARNING (not a hard error — operators may copy-paste
      labels and forget to remove the Anthropic-only field).
      Empty-string default means "use the host's live OAuth file".

    * :attr:`api_key` — OPTIONAL. Stores the RAW value (env-ref or
      literal) as authored in spec.yaml. Three accepted forms (see
      :mod:`config._env_var_substitution` for parsing semantics):

        1. ``$VAR`` or ``${VAR}`` — env-var reference, resolved at
           agent START (PR B). PR A only recognizes the shape; the
           validator does NOT resolve the env var because the spec
           may be loaded on a different host than where the agent
           will run.

        2. Literal string (``sk-ant-...``) — accepted but emits a
           stderr warning at start: "secrets in spec.yaml is
           anti-pattern; spec.yaml syncs via dotfiles git". PR A
           surfaces this warning at VALIDATE time as a non-fatal
           hint string.

        3. Omitted (empty string default) — falls back to the
           provider registry's ``auth_token_env`` (the existing
           PR #244 mechanism). Most agents use this path.

      :attr:`api_key` and :attr:`account` are MUTUALLY EXCLUSIVE
      within a single label — an API-key backend needs no OAuth, and
      declaring both forces the runtime to guess which auth path
      wins. The validator rejects this combination with a clear
      message naming the offending label.

    Frozen on purpose: a parsed label is the operator's declared
    intent; mutating it post-parse is always a code smell (the
    runtime should rebuild the chain rather than patch a label
    in-place). Hashable via the frozen contract so labels can sit in
    sets when the dispatcher (PR B) tracks ``disabled-this-session``
    state.
    """

    provider: str = ""
    """Registered provider name (see
    :mod:`config._provider_registry`). Empty string is the parsed
    sentinel for "field missing"; the validator surfaces the loud
    "required field" error against the raw block — the parser stays
    non-raising so a partially malformed spec still produces an
    inspectable shape."""

    model_id: str = ""
    """Provider-specific model identifier. Empty string is the parsed
    sentinel for "field missing"; the validator surfaces the loud
    "required field" error against the raw block."""

    account: str = ""
    """Saved-account name (Anthropic Max OAuth selector) — same
    semantics as :class:`config._types.ClaudeSpec.account`. Empty =
    no OAuth pin. Mutually exclusive with :attr:`api_key`."""

    api_key: str = ""
    """RAW api-key value as authored in spec.yaml — env-var reference
    (``$VAR`` / ``${VAR}``) or literal. Empty = fall back to the
    provider registry's ``auth_token_env``. Mutually exclusive with
    :attr:`account`. PR A does NOT resolve env-var references; PR B
    does the at-start resolution + the literal-string stderr warning."""


# A ``ModelChain`` is just the parsed mapping ``label -> ModelLabel``.
# Insertion order IS the fallback cascade order — this is the only
# meaningful semantic this alias carries beyond ``dict[str, ModelLabel]``.
# Python dicts preserve insertion order since 3.7, and PyYAML's
# ``SafeLoader`` builds mappings in source order; parser callers MUST
# preserve that order on the way through (no sort, no re-key).
#
# Spelled as a ``type`` alias rather than ``TypeAlias`` to keep this
# module import-cycle-free with the runtime typing stack — none of
# the call sites (parser, validator, AgentConfig) need the runtime
# alias type; they all just declare ``dict[str, ModelLabel]`` in their
# annotations. The alias exists for documentation / re-export from
# :mod:`scitex_agent_container.config`.
ModelChain = dict[str, ModelLabel]
"""Parsed ``spec.model`` block: ``label-name -> ModelLabel``.

The mapping's insertion order IS the fallback cascade order (ADR-0018
§"Target shape"). Callers that iterate the chain (PR B dispatcher) MUST
iterate via ``chain.items()`` to honor that order; do NOT sort the
keys. Empty dict means "no v4 model chain configured" — the loader
falls back to the v3 ``spec.claude.*`` shape via the parser's v3-alias
path (see :mod:`config._parsers._model_chain`)."""


__all__ = ["ModelLabel", "ModelChain"]
