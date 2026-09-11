"""``spec.engines`` validation — everything decidable from the spec TEXT.

The split is deliberate and load-bearing (see ``_engine_types``'s module
docstring): what the spec text alone decides is a LOAD error here; what
the HOST decides (an unset ``$API_KEY``, an endpoint that will not
answer) is a START-path refusal in :mod:`_lifecycle._engine_select`.
Putting the host questions here would make ``sac agents list`` — which
loads every spec on the machine — answer a question nobody asked, once
per spec.

Sibling of ``_claude_validation`` / ``_provider_validation`` /
``_shape_validation``, called from ``_validation.validate_raw``.
"""

from __future__ import annotations

from typing import Mapping

from ._engine_entry_validation import validate_engine_entry
from ._engine_library import (
    load_fleet_library,
    resolve_engine_namespace,
    spec_engine_key,
)
from ._engine_types import (
    ENGINE_PIN_KEY,
    ENGINES_KEY,
    EngineDefaultError,
    default_engine,
    legacy_conflict_messages,
    parse_engines,
)

__all__ = ["validate_engine_pin", "validate_engines"]

def validate_engines(spec: dict, kind: object = "Agent") -> list[str]:
    """Return ``spec.engines`` errors (empty = valid).

    Covers, in order: the block shape, each engine key's spelling, each
    entry's fields, the exactly-one-default rule, and the legacy
    single-backend reconciliation (both-agreeing accepted, both
    disagreeing a hard error naming both values).
    """
    if not isinstance(spec, dict) or ENGINES_KEY not in spec:
        return []
    block = spec.get(ENGINES_KEY)
    if block is None:
        # Written null = "I know about engines and declare none" — the
        # explicit-spec posture, not an error.
        return []
    if not isinstance(block, Mapping):
        return [
            f"spec.{ENGINES_KEY} must be a mapping of "
            "`<engine-key>: {harness, model, provider, ...}` entries, got "
            f"{type(block).__name__}."
        ]
    if not block:
        return [
            f"spec.{ENGINES_KEY} is empty. Declare at least one engine, or "
            "remove the block and use the single-backend spec.claude "
            "surface."
        ]

    errors: list[str] = []
    for key, raw in block.items():
        errors += validate_engine_entry(
            str(key), raw, namespace=f"spec.{ENGINES_KEY}"
        )

    # ``spec.engine`` is the canonical explicit choice. The legacy
    # exactly-one-``default: true`` rule applies only when no pin is stated;
    # requiring both makes two separate fields select the same engine.
    if not spec_engine_key(spec):
        try:
            default_engine(parse_engines(spec))
        except EngineDefaultError as exc:
            errors.append(str(exc))

    errors += legacy_conflict_messages(spec)
    return errors


def validate_engine_pin(spec: dict, kind: object = "Agent") -> list[str]:
    """Return ``spec.engine`` errors (empty = valid).

    THREE THINGS ARE DECIDABLE FROM THE TEXT PLUS THE FLEET FILE, so all
    three are load errors and none of them evaporates:

      * the pin is not a string — a list or mapping there is a typo, not
        a backend;
      * the pin names an engine NEITHER this spec NOR the fleet library
        declares — the error lists both sources, because knowing WHICH
        file to edit is most of the fix;
      * the fleet library itself is unreadable, malformed, or names a
        default engine it does not declare. A library that cannot be
        read decides nothing, and a spec that would have followed it must
        not silently start on something else instead.

    A spec with no ``engine:`` line and a healthy (or absent) library
    produces nothing — which is every spec deployed today.
    """
    if not isinstance(spec, dict):
        return []

    errors: list[str] = []
    library = load_fleet_library()
    pinned_raw = spec.get(ENGINE_PIN_KEY, None)
    depends_on_library = bool(str(pinned_raw or "").strip()) or not parse_engines(spec)
    if library.errors and depends_on_library:
        errors += list(library.errors)

    if pinned_raw is None:
        return errors
    if not isinstance(pinned_raw, str):
        return errors + [
            f"spec.{ENGINE_PIN_KEY} must be a string naming ONE engine key "
            f"(e.g. `{ENGINE_PIN_KEY}: qwen38-27b`), got "
            f"{type(pinned_raw).__name__}. To declare the engine itself, use "
            f"the `{ENGINES_KEY}:` block; this key only CHOOSES."
        ]

    key = spec_engine_key(spec)
    if not key:
        # Written empty = "I know about the pin and state none" — the
        # explicit posture, and it falls through to the fleet default.
        return errors

    namespace = resolve_engine_namespace(spec)
    if key in namespace:
        return errors

    local = sorted(parse_engines(spec))
    fleet = sorted(library.engines)
    return errors + [
        f"spec.{ENGINE_PIN_KEY}={key!r} names an engine nothing declares. "
        f"This spec's own `{ENGINES_KEY}:` block declares: "
        f"{local or '(none)'}. The fleet engine library "
        f"({library.path}) declares: {fleet or '(none)'}. Add the engine to "
        "whichever of the two it belongs in — sac will not guess which "
        "backend was meant, and will not fall back to another one."
    ]
