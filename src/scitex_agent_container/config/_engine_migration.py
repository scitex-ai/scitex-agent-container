"""MIGRATION — the legacy single-backend block beside an ``engines:`` block.

THIS MODULE IS MEANT TO BE DELETED. It exists only because 123 agent
specs on compute-04 alone are written with the legacy single-backend
surface (``spec.harness`` + ``spec.claude.model`` +
``spec.claude.provider``), and a hard rename would boot-red every one of
them. It implements exactly the rule ``_harness_types`` already
implements for ``harness``/``provider``, applied to a BLOCK instead of a
key: legacy alone works silently, ``engines:`` alone is the new path,
BOTH agreeing is accepted silently, BOTH disagreeing is a hard error
naming both values.

THE END CONDITION is ``_engine_types.MIGRATION_END_CONDITION``: when
every deployed spec declares ``engines:``, this file is deleted and the
legacy block stops being read as a backend declaration at all. It is a
separate module rather than a section of ``_engine_types`` precisely so
that closing the migration is a file removal and not an archaeology
exercise.
"""

from __future__ import annotations

from typing import Any, Mapping

from ._engine_types import (
    ENGINES_KEY,
    MIGRATION_END_CONDITION,
    EngineDefaultError,
    EngineSpec,
    default_engine,
    parse_engines,
)
from ._harness_types import (
    HARNESS_KEY,
    LEGACY_HARNESS_KEY,
    HarnessKeyConflictError,
    declared_harness,
)
from ._provider_parse import provider_identity

__all__ = ["legacy_backend", "legacy_conflict_messages"]


def _stated(value: Any) -> str | None:
    """The STATED text of a YAML scalar, or ``None`` for "no opinion"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None




def legacy_backend(spec: Mapping) -> dict[str, Any]:
    """The backend the LEGACY single-backend block states, field by field.

    Values are ``None`` where the legacy block states no opinion, so
    "written but empty" (``model: ""``, ``harness: ~``) reads as no
    opinion and never manufactures a disagreement. ``harness`` goes
    through :func:`_harness_types.declared_harness`, so the
    ``harness``/``provider`` alias pair is already reconciled before it
    reaches the engine comparison — and its OWN conflict stays its own
    error rather than being reported twice.
    """
    claude = spec.get("claude") if isinstance(spec, Mapping) else None
    claude_block = claude if isinstance(claude, Mapping) else {}
    try:
        harness = declared_harness(spec)
    except HarnessKeyConflictError:
        harness = None
    return {
        "harness": harness,
        "model": _stated(claude_block.get("model")),
        "provider": claude_block.get("provider"),
    }


def _legacy_key_for(field_name: str) -> str:
    if field_name == "harness":
        return f"spec.{HARNESS_KEY} (or its alias spec.{LEGACY_HARNESS_KEY})"
    return f"spec.claude.{field_name}"


def legacy_conflict_messages(spec: Mapping) -> list[str]:
    """Disagreements between the legacy block and the DEFAULT engine.

    The comparison targets the DEFAULT engine because that is the
    backend the spec runs on with no ``--engine`` — the one the legacy
    block claims to describe. A NON-default engine naturally differs
    (that is the entire point of declaring several), so it is not
    compared and cannot conflict.

    THE COMPARISON IS ONE-SIDED, and deliberately: a legacy field that
    states nothing (absent, null, ``""``) declares no opinion and can
    never conflict, while a legacy field that STATES a value conflicts
    whenever the default engine's EFFECTIVE value differs — including
    when the engine's effective value is "nothing" (no model, no
    provider). That asymmetry follows from :func:`apply_engine` writing
    the whole triple unconditionally: an engine that declares no
    provider RUNS with no provider, so a legacy ``spec.claude.provider``
    sitting beside it is not inherited — it is contradicted, and saying
    so is the difference between a hard error and an agent quietly
    started on the wrong endpoint.

    Returns ``[]`` for a legacy-only spec, an engines-only spec, and a
    both-agreeing spec alike.
    """
    engines = parse_engines(spec)
    if not engines:
        return []
    try:
        engine = default_engine(engines)
    except EngineDefaultError:
        # The default itself is broken; that error is the validator's
        # first report and reporting a comparison against an
        # undetermined default on top of it would be noise.
        return []
    if engine is None:
        return []
    legacy = legacy_backend(spec)
    messages: list[str] = []

    for field_name, engine_value in (
        ("harness", engine.harness),
        ("model", engine.model),
    ):
        stated = legacy[field_name]
        if stated is None:
            continue
        if str(stated).strip().lower() != str(engine_value).strip().lower():
            messages.append(
                _conflict_message(
                    field_name, stated, engine, engine_value or None
                )
            )

    legacy_provider = provider_identity(legacy["provider"])
    engine_provider = provider_identity(engine.provider_declared)
    if legacy_provider is not None and legacy_provider != engine_provider:
        messages.append(
            _conflict_message(
                "provider",
                legacy["provider"],
                engine,
                engine.provider_declared,
            )
        )
    return messages


def _conflict_message(
    field_name: str, legacy_value: Any, engine: EngineSpec, engine_value: Any
) -> str:
    return (
        f"{_legacy_key_for(field_name)}={legacy_value!r} and "
        f"spec.{ENGINES_KEY}.{engine.key}.{field_name}={engine_value!r} "
        f"disagree. {engine.key!r} is this spec's DEFAULT engine, so both "
        "claim to describe the backend this agent starts on, and two "
        "different values means the spec does not say which — sac refuses "
        f"to guess. Either delete `{_legacy_key_for(field_name)}` (leave "
        "it empty/null to state no opinion) or make the two agree. The "
        "legacy single-backend block is accepted BESIDE `"
        f"{ENGINES_KEY}:` only while it says the same thing; the migration "
        f"ends when {MIGRATION_END_CONDITION}."
    )
