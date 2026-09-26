#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE ENGINE PRECEDENCE — which backend an agent runs, and who decided.

ONE function answers "which engine", and it is the only place in sac
that answers it. Extracted from ``_engine_types`` when the fleet engine
library gave the question a second source: a resolver that consults a
spec AND a fleet-wide file is no longer a property of the spec dataclass
module, and the per-file cap said so first.

THE ORDER IS THE DESIGN. Five steps, first hit wins, no step falling
back to a later one after failing:

    1. ``--engine <key>``            (start-time; handled by
                                      :func:`_engine_types.select_engine`)
    2. ``spec.engine: <key>``        the spec PINS itself
    3. a LEGACY ``spec.claude`` backend declaration — also a PIN
    4. ``engine:`` in the fleet engine library — THE FLEET DEFAULT
    5. nothing declared anywhere     → the harness's own backend

Step 3 sitting ABOVE step 4 is what makes the fleet library's landing
day a no-op: every spec deployed today carries a legacy declaration, so
every one of them is pinned to what it already runs, and the fleet
default becomes real ONE AGENT AT A TIME as the migration sweep clears
each pin. The other ordering repoints 130 agents the moment the file
appears.

NO VENDOR IS PRIVILEGED HERE. There is no branch for a vendor, no
sentinel value that means one, and no arm with no ``else``: every engine
is a row addressed by key, and the fleet default is a string read out of
a YAML file. Making Qwen the fleet default is a data edit; so is making
Claude the fleet default; neither costs a line of this module.
"""

from __future__ import annotations

from typing import Mapping

from ._engine_types import (
    ENGINE_PIN_KEY,
    ENGINES_KEY,
    EngineDefaultError,
    EngineSpec,
    UnknownEngineError,
)

__all__ = ["default_engine", "resolve_default", "resolve_default_for_spec"]


def resolve_default(
    engines: Mapping[str, EngineSpec],
    *,
    spec_engine_key: str = "",
    local_keys: "frozenset[str] | set[str] | None" = None,
    legacy_pinned: bool = False,
    fleet_default_key: str = "",
) -> EngineSpec | None:
    """THE PRECEDENCE — which engine this agent runs when ``--engine`` is absent.

    ``engines`` is the MERGED namespace (fleet library ∪ spec-local);
    ``local_keys`` says which of those the spec itself wrote. Five steps,
    first hit wins, and NO step ever falls back to a later one after
    failing:

      1. ``spec.engine: <key>`` — the spec PINS itself. Immune to any
         fleet edit. An unknown key raises rather than degrading.
      2. a spec-local entry marked ``default: true`` — the DEPRECATED
         spelling of step 1 (see :data:`ENGINE_ENTRY_KEYS`), honoured so
         the specs already written that way keep starting on the backend
         they start on today.
      3. a spec-local block with exactly ONE entry — no ceremony needed;
         one declared backend cannot be ambiguous. Several, none chosen,
         and the fleet default naming none of them → hard error: taking
         the first would make the backend depend on YAML ordering.
      4. a LEGACY backend declaration in ``spec.claude`` → ``None``. The
         legacy block IS a pin, as binding as step 1, so the fleet
         default does NOT reach past it.
      5. ``engine:`` in the fleet library — THE FLEET DEFAULT.

    WHY STEP 4 SITS ABOVE STEP 5, and why it is the load-bearing
    decision: on the day the fleet library first appears, every deployed
    spec still carries a legacy declaration, so every one of them is
    pinned to what it runs today and the new file changes NOTHING for
    anybody. The fleet default becomes real one agent at a time, as the
    migration sweep clears each legacy pin. The other ordering would
    repoint the whole fleet the moment the file was written.

    Nothing declared anywhere returns ``None`` — the state of a spec that
    runs the harness's own built-in backend (Claude Code on OAuth). That
    is a real, working configuration, not an omission, so it is not a
    refusal: making it one would red-start the corpus for declaring
    nothing new, the same posture ``residency`` and ``to_home_layers``
    took. A declaration that names something unresolvable DOES refuse —
    see steps 1 and 5.
    """
    local = frozenset(local_keys) if local_keys is not None else frozenset(engines)

    pinned = (spec_engine_key or "").strip()
    if pinned:
        if pinned not in engines:
            declared = ", ".join(repr(key) for key in engines) or "(none)"
            raise UnknownEngineError(
                f"spec.{ENGINE_PIN_KEY}={pinned!r} names an engine nothing "
                f"declares. Resolvable engines: {declared}. Declare it under "
                f"this spec's own `{ENGINES_KEY}:` block, or add it to the "
                "fleet engine library — sac will not guess which backend was "
                "meant."
            )
        return engines[pinned]

    local_engines = {key: engines[key] for key in engines if key in local}
    if local_engines:
        marked = [eng for eng in local_engines.values() if eng.is_default]
        if len(marked) > 1:
            names = ", ".join(repr(eng.key) for eng in marked)
            raise EngineDefaultError(
                f"spec.{ENGINES_KEY} marks {len(marked)} engines as "
                f"`default: true` ({names}); exactly one may. Delete "
                f"`default: true` from all of them and write `"
                f"{ENGINE_PIN_KEY}: <key>` once at the top of spec: instead."
            )
        if len(marked) == 1:
            return marked[0]
        if len(local_engines) == 1:
            return next(iter(local_engines.values()))
        if fleet_default_key and fleet_default_key in local_engines:
            return local_engines[fleet_default_key]
        names = ", ".join(repr(key) for key in local_engines)
        raise EngineDefaultError(
            f"spec.{ENGINES_KEY} declares {len(local_engines)} engines "
            f"({names}) but the spec does not say which one to start on, "
            f"and the fleet default names none of them. Add `"
            f"{ENGINE_PIN_KEY}: <key>` at the top of spec:. sac does not "
            "pick one for you — taking the first would make the backend "
            "depend on YAML ordering."
        )

    if legacy_pinned:
        return None

    fleet = (fleet_default_key or "").strip()
    if fleet:
        if fleet not in engines:
            declared = ", ".join(repr(key) for key in engines) or "(none)"
            raise EngineDefaultError(
                f"the fleet engine library's `{ENGINE_PIN_KEY}: {fleet!r}` "
                f"names an engine nothing declares. Resolvable engines: "
                f"{declared}."
            )
        return engines[fleet]
    return None


def default_engine(engines: Mapping[str, EngineSpec]) -> EngineSpec | None:
    """The SPEC-LOCAL default, ignoring the fleet library.

    The pre-fleet-library signature, kept because the migration
    reconciliation (``_engine_migration.legacy_conflict_messages``) and
    the validator both ask exactly this narrower question: "which of the
    engines THIS SPEC wrote does it start on?" — a question the fleet
    default cannot answer and must not contaminate.
    """
    if not engines:
        return None
    return resolve_default(engines, local_keys=frozenset(engines))


def resolve_default_for_spec(
    spec: Mapping, engines: Mapping[str, EngineSpec]
) -> EngineSpec | None:
    """:func:`resolve_default` with all four inputs read off ``spec``.

    The ONE place that knows how to ask a spec each precedence question:
    ``spec.engine`` (its own pin), which engine keys it wrote itself,
    whether its legacy ``spec.claude`` block still pins a backend, and
    what the fleet library defaults to.
    """
    from ._engine_library import fleet_default_key, local_engine_keys
    from ._engine_migration import legacy_backend

    legacy = legacy_backend(spec)
    pinned = ""
    if isinstance(spec, Mapping):
        pinned = str(spec.get(ENGINE_PIN_KEY) or "").strip()
    return resolve_default(
        engines,
        spec_engine_key=pinned,
        local_keys=local_engine_keys(spec),
        # The HARNESS is deliberately NOT part of this test. Every spec
        # states one, so counting it as a backend pin would pin all 131
        # of them forever and the fleet default could never become real
        # for anybody. The backend a legacy block declares is
        # model + provider; the harness is the OTHER axis.
        legacy_pinned=bool(legacy.get("model") or legacy.get("provider")),
        fleet_default_key=fleet_default_key(),
    )
