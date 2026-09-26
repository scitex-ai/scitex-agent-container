"""Cross-cutting helpers shared by ``parse_<section>`` functions.

Holds the dotted-key traversal, the model-name display map, the
${metadata.*} interpolator, and the small ``_parse_command_list``
normaliser that ``_startup`` and ``parse_startup_commands`` share.

Per-section parsers themselves enforce the ``raw or {}`` / ``not
isinstance(raw, dict)`` guards inline — those checks vary just enough
(some return empty default-constructed dataclasses, some fall back to a
legacy field) that abstracting them would obscure the per-section
contract.
"""

from __future__ import annotations

import re
from typing import Any

from .._types import StartupCommand

# All known hook keys. Unknown keys in the YAML are ignored (forward-compat).
HOOK_KEYS = (
    "pre_start",
    "post_start",
    "pre_stop",
    "post_stop",
    "on_compact",
    "on_restart",
    "on_diff",
)


# Model name mapping for auto-derived SCITEX_AGENT_CONTAINER_MODEL env var
MODEL_DISPLAY_NAMES: dict[str, str] = {
    "opus": "Claude Opus",
    "opus[1m]": "Claude Opus (1M)",
    "sonnet": "Claude Sonnet",
    "sonnet[1m]": "Claude Sonnet (1M)",
    "haiku": "Claude Haiku",
}

#: What ``model`` resolves to when nothing states one. Named rather than
#: repeated: it is applied in TWO places now — the legacy read in
#: ``_loaders`` and the engine fold in ``_engine_types.apply_engine`` — and
#: two spellings of a default is how they drift apart.
DEFAULT_MODEL = "sonnet"

#: The auto-derived env var carrying the DISPLAY form of the resolved model
#: into the container. Injected into every agent (``SAC_SPEC_ENV_KEYS``) and
#: printed by ``sac whoami``.
MODEL_ENV_KEY = "SCITEX_AGENT_CONTAINER_MODEL"


def resolve_model_surface(model: "str | None") -> "tuple[str, str]":
    """``(model, display)`` — the pair every read surface shows.

    One function because the two halves must never disagree: ``sac agents
    list`` prints the first and the container receives the second, and a
    model resolved twice by two rules is a fleet reporting one thing and
    running another.
    """
    resolved = str(model or "").strip() or DEFAULT_MODEL
    return resolved, MODEL_DISPLAY_NAMES.get(resolved, resolved)


def get_nested(data: dict, key: str, default: Any = None) -> Any:
    """Traverse a dot-separated key path in a nested dict."""
    keys = key.split(".")
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current


def interpolate_metadata(value: str, metadata: dict) -> str:
    """Replace ${metadata.*} references in a string value."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return metadata.get("name", m.group(0))
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            labels = metadata.get("labels", {}) or {}
            return labels.get(label, m.group(0))
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def _parse_command_list(raw: Any) -> list[StartupCommand]:
    out: list[StartupCommand] = []
    for item in raw or []:
        if isinstance(item, str):
            if item:
                out.append(StartupCommand(delay=0, command=item))
        elif isinstance(item, dict) and item.get("command"):
            try:
                delay = int(item.get("delay", 0))
            except (
                TypeError,
                ValueError,
            ):  # stx-allow: fallback (reason: type coercion or format mismatch)
                delay = 0
            out.append(StartupCommand(delay=delay, command=str(item["command"])))
    return out
