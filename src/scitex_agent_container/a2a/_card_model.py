"""Model-resolution helper for the AgentCard projection.

Lead directive 431365c (2026-06-08): the AgentCard surface MUST read
the resolved model id, not just ``spec.claude.model``. For provider
agents whose ``spec.claude.model`` is intentionally empty (the
operator wants to inherit the registry's default_model or override
it via Form B), the card was previously stuck showing ``null`` even
though the SDK was actually talking to a concrete model id. That made
the card useless for fleet-wide model-routing and observability.

This module owns the resolution chain. Kept separate from
``_card.py`` so the projection module stays under the 512-line cap.

Precedence chain (highest wins)
-------------------------------

  1. ``spec.claude.provider.model`` — Form C (custom) explicit
     model id in the spec YAML.
  2. ``spec.claude.provider.model`` for Form B (registry-name +
     override). Same key, different parent dict.
  3. Registry entry's ``default_model`` (for Form A bare-string and
     Form B without override). Looked up via the merged
     ``built-in + providers.d/`` registry so operator overlays
     are honoured.
  4. ``spec.claude.model`` — back-compat for non-provider agents
     and the legacy ``{base_url, auth_token_env}`` dict shape
     (which has no model field of its own).
  5. ``spec.model`` — v2 legacy fall-through.

Returns ``None`` when no step produces a model id. The card surfaces
``None`` as-is so a client can tell "no model declared" from
"model = <id>".
"""

from __future__ import annotations

from typing import Any


def resolve_card_model(spec: dict[str, Any]) -> str | None:
    """Return the model id the operator's agent is actually running.

    See the module docstring for the precedence chain. The function
    NEVER raises — a malformed registry overlay or unexpected dict
    shape degrades to "no model" and the next chain step takes over.
    The card is a best-effort observability surface; crashing the
    projection on a bad overlay would make the entire ``sac listen``
    AgentCard endpoint unavailable.
    """
    claude = spec.get("claude") or {}
    provider_block = claude.get("provider")
    # Steps 1 & 2 — explicit model on a dict-shape provider.
    if isinstance(provider_block, dict):
        explicit = provider_block.get("model")
        if isinstance(explicit, str) and explicit:
            return explicit
    # Step 3 — registry default for bare-string or Form B name without override.
    registry_name: str | None = None
    if isinstance(provider_block, str):
        registry_name = provider_block
    elif isinstance(provider_block, dict) and isinstance(
        provider_block.get("name"), str
    ):
        registry_name = provider_block.get("name")
    if registry_name:
        entry = _safe_registry_entry(registry_name)
        if entry is not None:
            default = entry.get("default_model")
            if isinstance(default, str) and default:
                return default
    # Step 4 — spec.claude.model (v3 fallback).
    claude_model = claude.get("model")
    if isinstance(claude_model, str) and claude_model:
        return claude_model
    # Step 5 — top-level spec.model (v2 fallback).
    spec_model = spec.get("model")
    if isinstance(spec_model, str) and spec_model:
        return spec_model
    return None


def _safe_registry_entry(name: str) -> dict[str, Any] | None:
    """Load the merged registry and return one entry, swallowing loader errors.

    The AgentCard projection must never crash on an overlay-loader
    error (a malformed providers.d/*.yaml on the host should surface
    via the parser path with a loud error, but card rendering happens
    over HTTP and an exception here would 500 the well-known endpoint
    for EVERY agent, not just the offending one). Best-effort fall
    through.
    """
    try:
        from ..config._provider_registry_d import load_merged_registry

        return load_merged_registry().get(name)
    except Exception:  # stx-allow: fallback (reason: see docstring; card rendering must not 500 on overlay errors)
        return None


__all__ = ["resolve_card_model"]
