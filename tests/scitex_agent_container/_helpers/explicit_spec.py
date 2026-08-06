"""Shared fixture scaffold for the explicit-spec red-start ruling.

Operator ruling 2026-07-21: EVERY spec field must be written explicitly
— an omitted field is a load error. Test fixtures therefore cannot ship
minimal specs anymore; this helper deep-merges a test's spec overrides
on top of the production paste-defaults map (the SSOT in
``config._explicit_validation``) so fixtures stay green as the required
map evolves, while each test still spells out only the fields it is
actually about.

The GREEN-path proof does NOT rely on this helper: the hand-written
fully-explicit YAML in ``tests/scitex_agent_container/config/
test__explicit_validation.py`` is authored field by field, so the map
and the fixtures cannot silently agree by construction.
"""

from __future__ import annotations

import copy
from typing import Any

from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)

__all__ = ["deep_merge", "explicit_doc", "explicit_spec", "explicitize_yaml"]


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` on top of ``base`` (override wins)."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def explicit_spec(
    overrides: dict[str, Any] | None = None, *, kind: str = "Agent"
) -> dict:
    """Full explicit ``spec`` mapping with ``overrides`` merged on top.

    Placement (``host``/``hosts``) is NOT defaulted here — it is a
    mutually-exclusive pair the caller must declare, exactly as in a
    real spec. Callers that don't care write ``{"host": "${HOSTNAME}"}``.
    """
    return deep_merge(explicit_spec_defaults(kind), overrides or {})


def explicitize_yaml(yaml_text: str) -> str:
    """Merge explicit-field defaults beneath a YAML doc's spec (spec wins).

    For string-template fixtures: keeps the fixture's readable minimal
    body as the authored surface while the dump satisfies the red-start
    validator. Comments are lost (fixture bodies only); placeholder
    tokens inside VALUES survive verbatim.

    Adds ``host: ${HOSTNAME}`` when the doc declares neither ``host`` nor
    ``hosts`` — the same terseness affordance :func:`explicit_doc` gives the
    dict path. The two were asymmetric until 2026-08-07 and that asymmetry
    silently voided tests: ``spec.host`` became REQUIRED on 2026-06-24, so a
    string-template fixture without it made ``load_config`` RAISE, the caller
    saw ``cfg is None``, and the code under test quietly took a different
    branch. Nothing failed — the tests kept reporting, on a path they were not
    written for. A fixture-migration pass on 2026-07-21 edited the very file
    holding one of them and still missed it, because a missed fixture and a
    correct one look identical from the outside.
    """
    import yaml

    doc = yaml.safe_load(yaml_text)
    kind = doc.get("kind", "Agent")
    spec = deep_merge(explicit_spec_defaults(kind), doc.get("spec") or {})
    if "host" not in spec and "hosts" not in spec:
        spec["host"] = "${HOSTNAME}"
    doc["spec"] = spec
    return yaml.safe_dump(doc, sort_keys=False)


def explicit_doc(
    spec_overrides: dict[str, Any] | None = None,
    *,
    kind: str = "Agent",
    metadata: dict | None = None,
) -> dict:
    """Full v3 YAML document with an explicit spec (+ default placement).

    Adds ``host: ${HOSTNAME}`` when the overrides carry neither ``host``
    nor ``hosts`` so single-purpose tests stay terse.
    """
    spec = explicit_spec(spec_overrides, kind=kind)
    if "host" not in spec and "hosts" not in spec:
        spec["host"] = "${HOSTNAME}"
    doc: dict[str, Any] = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": kind,
        "spec": spec,
    }
    if metadata is not None:
        doc["metadata"] = metadata
    return doc
