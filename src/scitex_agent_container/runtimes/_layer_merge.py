"""Deterministic deep-merge of layered ``to_home`` config (ADR-0018).

A SAC agent's effective ``.claude/settings.json`` / ``.mcp.json`` is
assembled from a precedence-ordered CASCADE of ``to_home`` layers,
LOWEST precedence first:

    user      ~/.scitex/agent-container/agents/_shared/to_home
    project   <proj>/.scitex/agent-container/agents/_shared/to_home
    per-agent <spec_dir>/to_home

Merge rules (one model for every mergeable config file):

  * ``dict`` + ``dict``        → recurse
  * the ``hooks`` block        → per-event concatenate + dedupe (additive;
                                 delegates to ``settings_json._merge_hooks_blocks``)
  * ``list`` + ``list``        → append uniques, order-preserving (additive)
  * ``_comment`` / ``_comment_*`` key → keep first layer's (self-describing
                                 documentation, not config; never a conflict)
  * scalar == scalar           → keep (idempotent; first layer owns it)
  * scalar != scalar           → raise :class:`LayerMergeConflict`

The conflict raise is the operator's SSOT rule (2026-06-24): each key is
owned by exactly ONE layer; two layers assigning the same key two
different scalar values has no safe winner, so the deploy hard-aborts
rather than silently picking one (No-Surprise).

Provenance: :func:`deep_merge_layers` also returns a map of dotted
key-path → the layer name that owns each leaf, so ``sac agents explain``
can show WHERE each effective setting came from (drift made visible).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ._to_home_errors import LayerMergeConflict
from .settings_json import _merge_hooks_blocks

_HOOKS_KEY = "hooks"


def deep_merge_layers(
    layers: "list[tuple[str, dict]]",
) -> "tuple[dict, dict[str, str]]":
    """Deep-merge ``layers`` (lowest precedence first) into one dict.

    ``layers`` is an ordered list of ``(layer_name, data)`` pairs; a
    non-dict ``data`` is skipped. Returns ``(merged, provenance)`` where
    ``provenance`` maps a dotted key-path to the ``layer_name`` that owns
    its leaf (``hooks`` / merged lists are tagged ``"(merged)"``).

    Raises :class:`LayerMergeConflict` when two layers assign the same
    scalar key two different values.
    """
    merged: dict = {}
    provenance: dict[str, str] = {}
    for name, data in layers:
        if not isinstance(data, dict):
            continue
        _merge_into(merged, provenance, data, name, "")
    return merged, provenance


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _record_subtree(prov: dict[str, str], path: str, value: Any, layer: str) -> None:
    """Tag every leaf under a freshly-added subtree with its owning layer."""
    if isinstance(value, dict) and value:
        for k, sub in value.items():
            _record_subtree(prov, _join(path, k), sub, layer)
    else:
        prov[path] = layer


def _merge_into(
    acc: dict,
    prov: dict[str, str],
    overlay: dict,
    layer: str,
    prefix: str,
) -> None:
    for key, val in overlay.items():
        path = _join(prefix, key)

        # ``hooks`` block — additive per-event merge (never a conflict).
        if key == _HOOKS_KEY and isinstance(val, dict):
            if isinstance(acc.get(key), dict):
                acc[key] = _merge_hooks_blocks(acc[key], val)
                prov[path] = "(merged)"
            else:
                acc[key] = deepcopy(val)
                prov[path] = layer
            continue

        if key not in acc:
            acc[key] = deepcopy(val)
            _record_subtree(prov, path, val, layer)
            continue

        cur = acc[key]
        # Documentation keys (``_comment``, ``_comment_*``) are self-describing
        # prose, not config. Two layers each carrying their own ``_comment``
        # must NOT hard-fail the cascade — keep the first (lowest) layer's value
        # (layer-local; same ownership rule as an idempotent scalar), regardless
        # of value type, so a doc key never routes into the conflict raise
        # below. (paper-scitex-clew 2026-07-06)
        if isinstance(key, str) and key.startswith("_comment"):
            continue
        if isinstance(cur, dict) and isinstance(val, dict):
            _merge_into(cur, prov, val, layer, path)
        elif isinstance(cur, list) and isinstance(val, list):
            for item in val:
                if item not in cur:
                    cur.append(item)
            prov[path] = "(merged)"
        elif cur == val:
            continue  # idempotent — first layer keeps ownership
        else:
            owner = prov.get(path, "(earlier layer)")
            raise LayerMergeConflict(
                f"to_home config conflict at '{path}': layer '{owner}' sets "
                f"{cur!r} but layer '{layer}' sets {val!r}. Each key must be "
                f"owned by exactly ONE layer (no silent override). "
                f"Fix: set '{path}' in only one cascade layer."
            )


__all__ = ["deep_merge_layers"]
