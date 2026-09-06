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

import re
from typing import Mapping

from ._engine_library import (
    load_fleet_library,
    resolve_engine_namespace,
    spec_engine_key,
)
from ._engine_types import (
    ENGINE_ENTRY_KEYS,
    ENGINE_PIN_KEY,
    ENGINES_KEY,
    EngineDefaultError,
    default_engine,
    legacy_conflict_messages,
    parse_engines,
)
from ._harness_types import is_known_harness, list_harnesses
from ._provider_validation import validate_provider

__all__ = ["validate_engine_pin", "validate_engines"]

# Engine keys name a backend on a command line
# (``--engine qwen38-27b``), so they stay shell-plain. Dots are allowed
# because model ids carry them (the operator's own example was
# ``--engine qwen-3.8-27b``).
_ENGINE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_REASONING_EFFORTS = ("none", "low", "medium", "high")


def _validate_entry(key: str, raw: object) -> list[str]:
    path = f"spec.{ENGINES_KEY}.{key}"
    if not isinstance(raw, Mapping):
        return [
            f"{path} must be a mapping of engine fields "
            f"({', '.join(sorted(ENGINE_ENTRY_KEYS))}), got "
            f"{type(raw).__name__}."
        ]
    errors: list[str] = []
    unknown = sorted(set(map(str, raw)) - ENGINE_ENTRY_KEYS)
    if unknown:
        errors.append(
            f"{path} has unknown field(s): {', '.join(unknown)}. An engine "
            f"entry carries {sorted(ENGINE_ENTRY_KEYS)} — the SAME fields "
            "the single-backend surface carries, plus the per-engine "
            "parameters. Anything else belongs in spec.extensions."
        )

    harness = raw.get("harness")
    if harness is not None and str(harness).strip():
        if not is_known_harness(str(harness).strip().lower()):
            errors.append(
                f"{path}.harness must be one of {list_harnesses()} (got "
                f"{str(harness)!r}). It resolves through the SAME harness "
                "registry as spec.harness — an engine cannot invent a "
                "harness the fleet cannot run."
            )

    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        errors.append(
            f"{path}.model must be a string, got {type(model).__name__}."
        )

    if "provider" in raw:
        # Reuses the single-backend surface's validator verbatim so the
        # accepted provider vocabulary cannot drift between the two.
        errors += [
            msg.replace("spec.claude.provider", f"{path}.provider")
            for msg in validate_provider(raw.get("provider"))
        ]

    default = raw.get("default")
    if default is not None and not isinstance(default, bool):
        errors.append(
            f"{path}.default must be true or false, got "
            f"{type(default).__name__}. (DEPRECATED: `{ENGINE_PIN_KEY}: "
            f"<key>` at the top of spec: says the same thing without making "
            "the CHOICE a property of the CHOSEN.)"
        )

    effort = raw.get("reasoning_effort")
    if effort is not None and str(effort).strip():
        if str(effort).strip().lower() not in _REASONING_EFFORTS:
            errors.append(
                f"{path}.reasoning_effort must be one of "
                f"{list(_REASONING_EFFORTS)} (got {str(effort)!r})."
            )

    max_ctx = raw.get("max_context_tokens")
    if max_ctx is not None:
        if isinstance(max_ctx, bool) or not isinstance(max_ctx, int):
            errors.append(
                f"{path}.max_context_tokens must be a positive integer, got "
                f"{type(max_ctx).__name__}."
            )
        elif max_ctx <= 0:
            errors.append(
                f"{path}.max_context_tokens must be > 0, got {max_ctx}."
            )

    env = raw.get("env")
    if env is not None and not isinstance(env, Mapping):
        errors.append(
            f"{path}.env must be a mapping of env var name → value, got "
            f"{type(env).__name__}."
        )
    return errors


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
        if not _ENGINE_KEY_RE.match(str(key)):
            errors.append(
                f"spec.{ENGINES_KEY} key {str(key)!r} is not a usable engine "
                "name. It is typed on a command line (--engine <key>), so it "
                "must start alphanumeric and contain only letters, digits, "
                "'.', '_' and '-'."
            )
        errors += _validate_entry(str(key), raw)

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
