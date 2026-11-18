"""Retain explicitly configured Hermes hooks when rebuilding model profiles.

Slice 2 (neutral layers): :func:`apply_neutral_hooks` additionally merges
hooks declared in the operator's neutral user layer (ADR-0033). Doctrine
boundary (skill 47): this renders only what the layer author declares and
grants nothing — policy/selection stays with the author (scitex-dev / leaf
packages own the default bindings, carried first via :func:`preserve_hook_config`);
Hermes authorizes at runtime. Only the ``hooks`` key is ever touched;
model, credentials, and every other key pass through byte-identical.
"""

from pathlib import Path

import yaml


def preserve_hook_config(profile: Path, document: dict) -> dict:
    """Carry hook configuration only; model and credential settings are regenerated.

    The profile can contain authored to_home settings or previously installed
    runtime bindings. This adapter owns no leaf policy and grants no command
    approval. Hermes still validates and authorizes the retained hooks.
    """
    path = profile / "config.yaml"
    if not path.is_file():
        return document
    try:
        previous = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise ValueError(
            "Cannot preserve Hermes hooks from unreadable configuration"
        ) from None
    if not isinstance(previous, dict) or "hooks" not in previous:
        return document
    hooks = previous["hooks"]
    if hooks is not None and not isinstance(hooks, dict):
        raise ValueError("Hermes hook configuration must be a mapping")
    return dict(document, hooks=hooks or {})


def merge_neutral_hooks(
    preserved: dict | None, neutral: dict[str, list[dict]]
) -> dict:
    """Merge neutral-rendered hooks under preserved native bindings.

    Preserved entries (explicitly installed bindings) come first per event;
    a neutral entry whose ``command`` already runs on that event is skipped
    (one firing per command). Either side may be empty; a missing ``hooks``
    key is treated as no hooks.
    """
    merged: dict[str, list[dict]] = {}
    for event, handlers in (preserved or {}).items():
        merged[event] = list(handlers or [])
    for event, handlers in neutral.items():
        seen = {h.get("command") for h in merged.setdefault(event, [])}
        merged[event].extend(h for h in handlers if h.get("command") not in seen)
    return merged


def apply_neutral_hooks(profile: Path, document: dict, *, layer_root=None) -> dict:
    """Preserve profile hooks, then merge the neutral user layer's hooks.

    ``layer_root`` overrides the user layer dir (tests); default is the
    operator's neutral user layer. A missing layer, a layer declaring no
    hooks, or hooks for other harnesses all reduce to
    :func:`preserve_hook_config` output exactly. A malformed layer raises
    (fail-loud: a silent no-op here would reproduce the missing-hook bug
    class this exists to fix).
    """
    from scitex_agent_container._neutral_config import load_layer
    from scitex_agent_container._neutral_config._hermes import (
        hermes_event,
        render_hooks,
    )
    from scitex_agent_container._neutral_config._layers import user_layer_root

    kept = preserve_hook_config(profile, document)
    root = layer_root if layer_root is not None else user_layer_root()
    layer = load_layer(root)
    hooks = [h for h in layer.hooks if h.name not in layer.disabled_hooks]
    hooks = [h for h in hooks if hermes_event(h) is not None]
    if not hooks:
        return kept
    return dict(
        kept, hooks=merge_neutral_hooks(kept.get("hooks"), render_hooks(hooks))
    )
