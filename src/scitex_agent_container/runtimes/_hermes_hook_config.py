"""Retain explicitly configured Hermes hooks when rebuilding model profiles."""

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
