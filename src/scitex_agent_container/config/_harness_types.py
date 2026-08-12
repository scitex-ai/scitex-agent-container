"""The HARNESS axis — ``spec.harness`` (canonical), ``spec.provider`` (alias).

WHICH AGENT PROGRAM RUNS THE LOOP, not which model endpoint answers.
``anthropic`` = the ``claude-agent-sdk``; ``openai`` = the
``openai-agents`` SDK. That is a HARNESS, so the key is ``harness``.

WHY THIS MODULE EXISTS AT ALL — the axis used to be spelled
``spec.provider`` and lived in :mod:`config._provider_types`, one file
away from :class:`config._provider_types.ProviderSpec`, which serves
``spec.claude.provider``: a genuine INFERENCE-provider override (an
Anthropic-compatible ``base_url`` + API key). Two unrelated axes shared
one word, and that file carried a standing "NAMING COLLISION WARNING"
telling every reader not to conflate them. PR #1027 made the same
argument one layer up (``ProviderSession`` → ``HarnessSession``): the
Protocol's implementations are agent PROGRAMS, not inference providers.
This module carries that argument into the spec surface, and splitting
the harness axis into its own file retires the collision rather than
documenting it — ``_provider_types`` is now only the inference axis.

MIGRATION, NOT RENAME. ``spec.provider`` is spec-facing YAML that the
live fleet's specs are written in, so a hard rename would boot-red every
one of them. Both keys are accepted:

  * ``harness:`` alone       → canonical; no warning.
  * ``provider:`` alone      → same behaviour, DEPRECATED. The warning
    fires on the real start path (see
    ``_lifecycle._runtime_select.warn_if_legacy_harness_key``), never in
    the loader: ``sac agents list`` loads every definition on the host,
    so a load-time warning would print one line per spec on a command
    nobody asked a style question of, and would contaminate
    ``--json`` output. This is the same ruling that placed
    ``warn_if_legacy_apptainer_runtime`` at the start path.
  * BOTH, agreeing         → accepted, silently. The author has
    migrated and kept the alias for an older sac; nothing is ambiguous.
  * BOTH, disagreeing      → HARD ERROR naming both values. Picking one
    silently would let a spec claim one harness and run another, which
    is exactly the class of lie the explicit-spec ruling exists to stop.

A key written with NO value (``provider:`` / ``provider: ~``) declares
no opinion — it satisfies the explicit-fields gate without asserting a
harness — so it never conflicts, and the other key wins. Only two
STATED values can disagree.
"""

from __future__ import annotations

from typing import Literal, Mapping

__all__ = [
    "AGENT_HARNESSES",
    "AgentHarness",
    "DEFAULT_AGENT_HARNESS",
    "HARNESS_KEY",
    "LEGACY_HARNESS_KEY",
    "HarnessKeyConflictError",
    "declared_harness",
    "harness_key_conflict_error",
    "is_known_harness",
    "list_harnesses",
    "resolve_spec_harness",
    "uses_legacy_harness_key",
]

#: The canonical top-level spec key for this axis.
HARNESS_KEY = "harness"

#: The DEPRECATED alias still honoured for the existing spec corpus.
LEGACY_HARNESS_KEY = "provider"

AgentHarness = Literal["anthropic", "openai"]

DEFAULT_AGENT_HARNESS: AgentHarness = "anthropic"

#: The closed set of agent harnesses sac knows how to run a session
#: through at all. ``"openai"`` validates and resolves; the concrete
#: runner landed with openai-compat-2/3.
AGENT_HARNESSES: tuple[str, ...] = ("anthropic", "openai")


class HarnessKeyConflictError(ValueError):
    """``spec.harness`` and ``spec.provider`` both stated, and they differ."""


def is_known_harness(name: str) -> bool:
    """True when ``name`` is a recognized harness."""
    return name in AGENT_HARNESSES


def list_harnesses() -> list[str]:
    """Return the recognized harnesses, sorted.

    Used by the spec validator's "unknown harness" error so the operator
    sees the exact set they can pick from without reading the source.
    """
    return sorted(AGENT_HARNESSES)


def _stated(spec: Mapping, key: str) -> str | None:
    """The STATED value of ``spec[key]``, or ``None`` for "no opinion".

    ``None`` covers three cases that all mean the same thing: the key is
    absent, the key is present with a null value, or the key is present
    with an empty/whitespace string. Normalised (stripped + lowercased)
    so ``Anthropic`` and ``anthropic`` are one value rather than a
    spurious conflict — the VALUE-legality check owns the casing
    diagnostic, this function only owns identity.
    """
    if not isinstance(spec, Mapping) or key not in spec:
        return None
    raw = spec[key]
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def declared_harness(spec: Mapping) -> str | None:
    """The harness this spec states, or ``None`` when it states none.

    Raises :class:`HarnessKeyConflictError` when the two keys state
    different harnesses — see the module docstring.
    """
    canonical = _stated(spec, HARNESS_KEY)
    legacy = _stated(spec, LEGACY_HARNESS_KEY)
    if canonical is not None and legacy is not None and canonical != legacy:
        raise HarnessKeyConflictError(_conflict_message(canonical, legacy))
    return canonical if canonical is not None else legacy


def resolve_spec_harness(spec: Mapping) -> str:
    """The harness for this spec, defaulting when it states none.

    Raises :class:`HarnessKeyConflictError` on a stated disagreement.
    """
    return declared_harness(spec) or DEFAULT_AGENT_HARNESS


def uses_legacy_harness_key(spec: Mapping) -> bool:
    """True when this spec reaches the axis ONLY through ``spec.provider``.

    Presence of the canonical key — even written null — means the author
    knows about ``harness`` and is not owed the deprecation nudge.
    """
    if not isinstance(spec, Mapping):
        return False
    return HARNESS_KEY not in spec and LEGACY_HARNESS_KEY in spec


def _conflict_message(canonical: str, legacy: str) -> str:
    return (
        f"spec.{HARNESS_KEY}={canonical!r} and spec.{LEGACY_HARNESS_KEY}="
        f"{legacy!r} disagree. ``{LEGACY_HARNESS_KEY}`` is the DEPRECATED "
        f"alias of ``{HARNESS_KEY}`` (they select the agent harness: which "
        "agent SDK runs the session — NOT spec.claude.provider, which points "
        "the Claude SDK at an Anthropic-compatible inference backend). Two "
        "different values means the spec does not say which harness it wants, "
        f"and sac refuses to guess: delete the ``{LEGACY_HARNESS_KEY}:`` line "
        f"and keep ``{HARNESS_KEY}: <the one you meant>``."
    )


def harness_key_conflict_error(spec: Mapping) -> list[str]:
    """List-form surface for ``validate_raw``: 0 or 1 conflict message."""
    try:
        declared_harness(spec)
    except HarnessKeyConflictError as exc:
        return [str(exc)]
    return []
