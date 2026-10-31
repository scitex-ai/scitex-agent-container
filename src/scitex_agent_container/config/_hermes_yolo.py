"""Hermes yolo mode from the selected harness (operator order 2026-09-29)."""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness


def parse_selected_hermes_yolo(spec: Mapping) -> bool:
    """Return the selected Hermes yolo policy.

    ``yolo`` bypasses all dangerous-command approval prompts
    (``hermes chat --yolo``). It is opt-in per spec: without it agents
    stop on every approval prompt, which strands unattended fleet
    agents. Defaults off so a spec that never considered the risk does
    not silently gain it.
    """
    if canonical_harness(resolve_spec_harness(spec)) != "hermes":
        return False
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return False
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "hermes" and isinstance(value, Mapping):
            return bool(value.get("yolo", False))
    return False


__all__ = ["parse_selected_hermes_yolo"]
