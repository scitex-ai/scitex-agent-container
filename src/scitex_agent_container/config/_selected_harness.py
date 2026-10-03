"""Per-call selection of an explicitly available harness, without spec edits."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from ._harness_lookup import canonical_harness


def project_declared_harness(
    raw: object, requested: str, *, engine: str | None = None
) -> dict:
    """Copy a declaration and select one existing available-harness block.

    The regular loader still validates the whole selected projection. Neither
    the parsed-spec cache nor the canonical document/default is changed.
    """
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("selected-harness-required")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("spec"), Mapping):
        raise ValueError("selected-harness-document-refused")
    available = raw["spec"].get("available_harnesses")
    selected = canonical_harness(requested)
    if not isinstance(available, Mapping):
        raise ValueError("selected-harness-not-declared")
    entries = [
        key
        for key, value in available.items()
        if canonical_harness(str(key)) == selected and isinstance(value, Mapping)
    ]
    if len(entries) != 1:
        raise ValueError("selected-harness-not-declared-or-ambiguous")
    result = deepcopy(dict(raw))
    result["spec"]["harness"] = selected
    if engine is not None:
        if not isinstance(engine, str) or not engine.strip():
            raise ValueError("selected-harness-engine-required")
        engines = raw["spec"].get("available_engines", raw["spec"].get("engines"))
        if not isinstance(engines, Mapping) or engine not in engines:
            raise ValueError("selected-harness-engine-not-declared")
        result["spec"]["engine"] = engine
    return result
