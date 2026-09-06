"""``spec.engines`` — SEVERAL declared backends, ONE picked at START.

WHY THIS EXISTS — a spec can declare exactly ONE backend today
(``spec.harness`` + ``spec.claude.model`` + ``spec.claude.provider``),
so switching an agent between Claude and a local Qwen means EDITING the
spec. The operator asked (Telegram 2026-09-03) for one spec to write
several backends down and pick one at start:

    sac agents restart <agent> --engine qwen38-27b

FOUR OPERATOR ANSWERS this module implements, verbatim in intent:

  Q1 VOCABULARY — the words are ENGINE and HARNESS. ``claude:`` was
     already the wrong name the moment it held Qwen (a vendor name is a
     claim about scope), so the multi-backend surface is ``engines:``
     and each entry is an ENGINE: one named (harness, model, provider,
     parameters) tuple.
  Q2 GRANULARITY — START TIME ONLY (「起動時だけで大丈夫です」). There
     is no per-turn escape hatch and no mid-session rebinding: the
     selected engine is folded into the resolved ``AgentConfig`` before
     the runtime is built and never consulted again.
  Q3 UNHONOURABLE ENGINE — REFUSE TO START, naming what could not be
     honoured (「勝手なフォールバックはしないと言うルールなので」).
     The spec value is the default, ``--engine`` overrides it, and
     NOTHING silently falls back — least of all onto the default when
     an explicit ``--engine`` was given. The refusal itself lives on the
     start path (:mod:`_lifecycle._engine_select`); this module owns the
     static shape.
  Q4 PARAMETERS — YES, per model, ``reasoning_effort`` above all (Qwen
     is expected to run at low effort permanently).

NO SECOND VOCABULARY. An engine entry carries the SAME fields the
single-backend surface carries: ``harness`` resolves through
:mod:`_harness_registry` (not a second list), ``provider`` folds through
:func:`_provider_parse.parse_provider_value` (not a second dataclass),
``model`` is the same model id. Only the two parameters
(``reasoning_effort`` / ``max_context_tokens``) and the ``default:``
marker are new words, because they name things the old surface could
not say.

MIGRATION, NOT RENAME — exactly the rule ``_harness_types`` already
implements for ``harness``/``provider``, applied to a BLOCK instead of
a key. 123 live specs on compute-04 alone are written with the legacy
single-backend block, so a hard rename boot-reds every one of them:

  * legacy block alone  → works unchanged, and SILENTLY. No deprecation
    line: at ~123 specs a nudge-per-start is noise, and the operator
    asked for "keeps working unchanged and silently".
  * ``engines:`` alone  → the new path; the default engine is folded
    into the config at LOAD time so every read surface (explain, birth
    certificate, status) sees the effective backend.
  * BOTH, agreeing      → accepted, silently. The author has migrated
    and kept the legacy block for an older sac; nothing is ambiguous.
  * BOTH, disagreeing   → HARD ERROR naming both values (see
    :func:`legacy_conflict_messages`). Picking one silently would let a
    spec claim one backend and run another.

WHERE THE DIAGNOSTICS FIRE. Everything decidable from the spec text
alone (two defaults, no default, unknown harness, malformed provider,
legacy disagreement) is a VALIDATOR error — ``validate_raw``, at load,
same as every other spec-shape error. Everything that depends on the
HOST (is ``$QWEN_API_KEY`` exported? does the endpoint answer?) fires on
the START path only, never in the loader: ``sac agents list`` loads
every spec on the host, so a loader-side host probe would probe N
endpoints for a command nobody asked a reachability question of, and a
loader-side warning would print one line per spec and contaminate
``--json``. That is the same ruling that placed
``warn_if_legacy_harness_key`` and ``warn_if_legacy_apptainer_runtime``
on the start path.

THE MIGRATION HAS AN END, and this is it: when every deployed spec
declares ``engines:`` (the roll-over is a separate dry-run-first sweep —
this module ships the mechanism only), the legacy single-backend
reading in :func:`legacy_backend` is deleted, ``spec.claude.model`` /
``spec.claude.provider`` / ``spec.harness`` stop being read as a
backend declaration, and :func:`legacy_conflict_messages` goes with
them. Until that sweep completes the legacy block is not deprecated —
it is the majority spelling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ._provider_parse import parse_provider_value
from ._provider_types import ProviderSpec

__all__ = [
    "ENGINES_KEY",
    "ENGINE_PIN_KEY",
    "ENGINE_ENTRY_KEYS",
    "MIGRATION_END_CONDITION",
    "EngineDefaultError",
    "EngineError",
    "EngineSpec",
    "UnknownEngineError",
    "apply_default_engine",
    "apply_engine",
    "default_engine",
    "resolve_default",
    "legacy_backend",
    "legacy_conflict_messages",
    "parse_engines",
    "select_engine",
]

#: The top-level spec key holding the named engines.
ENGINES_KEY = "engines"

#: THE WHOLE GRAMMAR CHANGE, one scalar: the top-level spec key that PINS
#: which engine this agent runs (``spec.engine: qwen38-27b``). Spelled
#: identically at the top of the fleet engine library, where it names the
#: FLEET default, because it means the same thing in both places: "the
#: engine to run when nothing more specific is stated". Flipping one
#: agent onto another backend is this ONE line; flipping the whole fleet
#: is this ONE line in the library.
ENGINE_PIN_KEY = "engine"

#: The keys ONE engine entry may carry. ``harness`` / ``model`` /
#: ``provider`` are the single-backend surface's own words; the rest are
#: the per-engine parameters (Q4).
#:
#: ``default`` is DEPRECATED — superseded by the top-level
#: :data:`ENGINE_PIN_KEY` (``spec.engine: <key>``), which says the same
#: thing without making the CHOICE a property of the CHOSEN. It stays
#: accepted, silently, for the specs already written with it: a hard
#: removal would boot-red them, and this axis exists to make switching
#: backends cheap, not to make it a flag day. It is deleted in the same
#: PR that deletes the legacy single-backend reading — see
#: :data:`MIGRATION_END_CONDITION`.
#:
#: ``harness`` inside an entry is deprecated for the same window and for
#: a sharper reason: after the split, an entry that states a harness is
#: an ENGINE claiming ownership of the HARNESS axis, which is precisely
#: the coupling this design removes. It survives only so a spec that
#: already writes it keeps loading.
ENGINE_ENTRY_KEYS = frozenset(
    {
        "harness",
        "model",
        "provider",
        "default",
        "reasoning_effort",
        "max_context_tokens",
        "env",
    }
)

#: What CLOSES this migration — stated so the compatibility window is a
#: condition, not an open-ended "someday". See the module docstring.
MIGRATION_END_CONDITION = (
    "every deployed spec declares spec.engines; then the legacy "
    "single-backend reading (spec.harness + spec.claude.model + "
    "spec.claude.provider as a BACKEND declaration) is deleted along "
    "with legacy_conflict_messages()"
)


class EngineError(ValueError):
    """Base class for ``spec.engines`` faults."""


class EngineDefaultError(EngineError):
    """``spec.engines`` does not name exactly one default entry."""


class UnknownEngineError(EngineError):
    """``--engine <key>`` named an engine the spec does not declare."""


@dataclass(frozen=True)
class EngineSpec:
    """ONE named backend an agent may start on.

    Every field is the single-backend surface's own field, so an engine
    entry reads as "the ``harness`` + ``claude`` block I would have
    written, given a name". ``provider_declared`` keeps the RAW YAML
    value beside the folded :class:`ProviderSpec` because the two carry
    different information: the fold collapses both an unregistered name
    and the ``anthropic`` sentinel to ``None``, and the refusal path has
    to tell those apart to say WHAT could not be honoured.

    THE HARNESS FIELD IS NULLABLE, AND THAT NULL IS THE WHOLE
    HARNESS/ENGINE SPLIT. It used to default to
    ``DEFAULT_AGENT_HARNESS``, which meant an entry that said nothing
    about the harness SAID ANTHROPIC — and :func:`apply_engine` then
    wrote that manufactured value over a spec that had declared
    ``harness: codex``. One engine definition could therefore not serve
    a Claude-Code agent and a Codex agent, which is exactly what a fleet
    engine library has to do. ``None`` now means NO OPINION: the engine
    names the model endpoint, the spec names the program that runs the
    loop, and neither overwrites the other.
    """

    key: str
    harness: str | None = None
    model: str = ""
    provider: ProviderSpec | None = None
    provider_declared: Any = None
    reasoning_effort: str = ""
    max_context_tokens: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    is_default: bool = False


def _stated(value: Any) -> str | None:
    """The STATED text of a YAML scalar, or ``None`` for "no opinion".

    Same three-cases-are-one rule as ``_harness_types._stated``: absent,
    null, and empty/whitespace all mean the author declared nothing.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _entry_mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _parse_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        # The validator owns the "must be a positive integer"
        # diagnostic; degrading to None here keeps an unvalidated
        # fixture path from crashing the loader.
        return None


def parse_engine_entry(key: str, raw: Any) -> EngineSpec:
    """Fold ONE ``spec.engines.<key>`` mapping into an :class:`EngineSpec`.

    Shape faults (non-mapping entry, unknown sub-key, bad types) are the
    VALIDATOR's diagnostic — see :mod:`_engine_validation`. This parser
    is deliberately total so a spec that is about to be rejected still
    parses far enough for the error message to quote it.
    """
    entry = _entry_mapping(raw)
    harness = _stated(entry.get("harness"))
    provider_declared = entry.get("provider")
    raw_env = entry.get("env")
    env: dict[str, str] = {}
    if isinstance(raw_env, Mapping):
        env = {str(k): str(v) for k, v in raw_env.items() if v is not None}
    return EngineSpec(
        key=key,
        harness=harness.lower() if harness else None,
        model=_stated(entry.get("model")) or "",
        provider=parse_provider_value(provider_declared),
        provider_declared=provider_declared,
        reasoning_effort=(_stated(entry.get("reasoning_effort")) or "").lower(),
        max_context_tokens=_parse_int(entry.get("max_context_tokens")),
        env=env,
        is_default=entry.get("default") is True,
    )


def parse_engines(spec: Mapping) -> dict[str, EngineSpec]:
    """Parse ``spec.engines`` into ``{key: EngineSpec}``, insertion-ordered.

    Returns ``{}`` when the block is absent, null, or not a mapping — a
    legacy-only spec parses to no engines and takes the unchanged path.
    """
    if not isinstance(spec, Mapping):
        return {}
    block = spec.get(ENGINES_KEY)
    if not isinstance(block, Mapping):
        return {}
    return {
        str(key): parse_engine_entry(str(key), value) for key, value in block.items()
    }


def default_engine(engines: Mapping[str, EngineSpec]) -> EngineSpec | None:
    """See :func:`_engine_precedence.default_engine`."""
    from ._engine_precedence import default_engine as _impl

    return _impl(engines)


def resolve_default(engines: Mapping[str, EngineSpec], **kwargs: Any):
    """See :func:`_engine_precedence.resolve_default`."""
    from ._engine_precedence import resolve_default as _impl

    return _impl(engines, **kwargs)


def select_engine(
    engines: Mapping[str, EngineSpec], requested: str | None
) -> EngineSpec | None:
    """The engine THIS start runs on.

    ``requested`` is the ``--engine`` argument (``None`` = not given, so
    the declared default wins). An unknown key raises
    :class:`UnknownEngineError` LISTING the declared keys — it never
    degrades to the default, because starting on a different backend
    than the one the operator named is the silent fallback Q3 forbids.
    """
    key = (requested or "").strip()
    if not key:
        return default_engine(engines)
    if not engines:
        raise UnknownEngineError(
            f"--engine {key!r} was given but the spec declares no "
            f"`{ENGINES_KEY}:` block, so there is no engine by that name "
            "to select. Either declare the engine in the spec or drop "
            "--engine to start on the spec's single declared backend."
        )
    if key not in engines:
        declared = ", ".join(repr(name) for name in engines)
        raise UnknownEngineError(
            f"--engine {key!r} resolves to no engine. Resolvable engines "
            f"(this spec's own `{ENGINES_KEY}:` block UNION the fleet "
            f"engine library): {declared}. Declare it in whichever of the "
            "two the key belongs to — `sac agents explain <agent>` prints "
            "the library path and which entries came from where. sac will "
            "NOT fall back to the default engine when an explicit --engine "
            "was given."
        )
    return engines[key]


def apply_engine(config: Any, engine: EngineSpec) -> None:
    """Fold ``engine`` onto a resolved ``AgentConfig``, in place.

    This is the ONLY place an engine becomes behaviour, and it writes
    the SAME fields the single-backend surface writes — which is what
    makes every downstream reader (auth argv, birth certificate,
    explain) engine-aware for free.

    AN ENGINE ENTRY FULLY DETERMINES THE BACKEND TRIPLE, and every one of
    the three is written UNCONDITIONALLY — including when the entry
    states nothing (``model: ""`` = "use the runtime default";
    ``provider: None`` = "no endpoint override"). Writing them
    conditionally is the obvious-looking version and it is wrong twice
    over: applying engine B after the loader applied default engine A
    would leave A's provider in place, silently pointing the agent at an
    endpoint B never declared; and a legacy ``spec.claude.provider``
    would leak into an engine that declares none. Both are exactly the
    "ran a backend nobody asked for" failure this axis exists to remove.
    A legacy block that states a value the default engine does not is
    therefore a CONFLICT, not an inheritance — see
    :func:`legacy_conflict_messages`.

    ``env`` merges ON TOP of ``spec.apptainer.env`` — a per-engine env
    entry is a deliberate override for THIS backend, so it wins over the
    agent-wide value it was written to displace.
    """
    config.engine_key = engine.key
    # THE SPLIT. An engine that states NO harness states no opinion, and
    # an opinion nobody stated must not be written over one that was:
    # this line used to assign a manufactured "anthropic" onto a spec
    # that had declared ``harness: codex``. Only a harness the entry
    # ACTUALLY names is applied — which is what lets ONE fleet engine
    # entry serve a Claude-Code agent and a Codex agent unchanged.
    if engine.harness is not None:
        config.harness = engine.harness
    config.reasoning_effort = engine.reasoning_effort
    config.max_context_tokens = engine.max_context_tokens
    claude = getattr(config, "claude", None)
    if claude is not None:
        claude.model = engine.model
        claude.provider = engine.provider
        # A provider-backed engine authenticates with the provider's API key;
        # the OAuth account the DEFAULT engine pins is not this start's, and
        # the runtime refuses the two together (_apptainer_provider:
        # "provider and account are mutually exclusive"). Measured
        # 2026-09-05: `business --engine qwen38-27b` was refused on the peer
        # with exactly that message while its spec was correct -- the fold
        # had left the account in place. Clearing it here keeps the rule
        # true for what actually runs; an engine without a provider keeps
        # the account, because OAuth is then the only auth it has.
        if engine.provider is not None:
            claude.account = ""
            # ...and the ROTATION POOL with it. The exclusion the runtime
            # enforces is provider-vs-OAuth, not provider-vs-``account``:
            # ``_claude_validation`` asserts it over ``credentials_files``
            # too, but only on the RAW spec, never on the folded config.
            # A spec declaring four credentials_files beside a
            # provider-backed engine therefore passed validation and then
            # composed the two at launch. Clearing both here keeps the
            # rule true for WHAT ACTUALLY RUNS.
            if hasattr(claude, "credentials_files"):
                claude.credentials_files = []
            if hasattr(claude, "credentials_file"):
                claude.credentials_file = ""
    if engine.env:
        merged = dict(getattr(config, "env", {}) or {})
        merged.update(engine.env)
        config.env = merged


def apply_default_engine(
    config: Any,
    engines: Mapping[str, EngineSpec],
    spec: Mapping | None = None,
) -> EngineSpec | None:
    """Fold the DEFAULT engine onto ``config`` at LOAD time; return it.

    PURE by design — no warning, no host probe, no network. ``sac agents
    list`` loads every spec on the machine, so a loader that warned would
    print one line per spec and contaminate ``--json``, and a loader that
    probed would dial N endpoints for a command that asked no
    reachability question. Both belong on the START path
    (:mod:`_lifecycle._engine_select`), the same ruling that placed
    ``warn_if_legacy_harness_key`` there.

    A malformed engines block (two defaults, none, an unknown harness, a
    legacy disagreement) is already a LOAD error from
    ``_engine_validation.validate_engines``, which runs first; the
    ``EngineDefaultError`` swallowed here is only reachable for a
    hand-built dict that skipped the validator, and leaving the config on
    its legacy fields is the honest outcome for a spec whose default
    could not be determined.
    """
    if not engines:
        return None
    from ._engine_precedence import default_engine as _spec_local_default
    from ._engine_precedence import resolve_default_for_spec

    try:
        if spec is None:
            # No spec handed over: answer the narrower SPEC-LOCAL
            # question. Reading the fleet default here would be worse
            # than useless — it would apply a fleet-wide backend to a
            # hand-built config whose legacy pin this function cannot
            # see, which is precisely the silent repointing the
            # precedence exists to prevent.
            selected = _spec_local_default(engines)
        else:
            selected = resolve_default_for_spec(spec, engines)
    except (EngineDefaultError, UnknownEngineError):
        return None
    if selected is not None:
        apply_engine(config, selected)
    return selected


# The MIGRATION half — reading the legacy single-backend block beside an
# ``engines:`` block and refusing when the two disagree — lives in the
# sibling ``_engine_migration`` module, which is meant to be DELETED
# when MIGRATION_END_CONDITION is met. Re-exported here so callers keep
# one import surface for the axis.
def legacy_backend(spec: Mapping) -> dict[str, Any]:
    """See :func:`_engine_migration.legacy_backend`."""
    from ._engine_migration import legacy_backend as _impl

    return _impl(spec)


def legacy_conflict_messages(spec: Mapping) -> list[str]:
    """See :func:`_engine_migration.legacy_conflict_messages`."""
    from ._engine_migration import legacy_conflict_messages as _impl

    return _impl(spec)
