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

import sys
from typing import Literal, Mapping

from ._harness_registry import known_harnesses

__all__ = [
    "AGENT_HARNESSES",
    "AgentHarness",
    "DEFAULT_AGENT_HARNESS",
    "HARNESS_KEY",
    "LEGACY_HARNESS_KEY",
    "HarnessKeyConflictError",
    "HarnessRuntimeMismatchError",
    "V4_HARNESS_DISPATCH_CARD",
    "declared_harness",
    "ensure_harness_matches_claude_launch",
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

# TYPING-ONLY, and the ONE spot a new harness still costs a hand edit:
# every RUNTIME set on this axis (AGENT_HARNESSES below,
# config._validation, runtimes._apptainer_provider) derives from the
# registry, but a `Literal` cannot be built from a runtime tuple without
# losing static checking, so the members are restated here. Keep in sync
# with the ``spec_harness`` values in config._harness_registry.
AgentHarness = Literal["anthropic", "openai", "codex"]

DEFAULT_AGENT_HARNESS: AgentHarness = "anthropic"

#: The closed set of agent harnesses sac knows how to run a session
#: through at all. ``"openai"`` validates and resolves; the concrete
#: runner landed with openai-compat-2/3. DERIVED from the harness
#: registry (v4 step 4, ``config._harness_registry``): the set is
#: whatever families the descriptor entries declare, so a new harness
#: is one registry entry, not an edit here. The registry module imports
#: NOTHING from this module at import time, so the derivation is not a
#: cycle (its reverse imports are call-time only).
AGENT_HARNESSES: tuple[str, ...] = known_harnesses()


class HarnessKeyConflictError(ValueError):
    """``spec.harness`` and ``spec.provider`` both stated, and they differ."""


class HarnessRuntimeMismatchError(RuntimeError):
    """A non-Anthropic harness was about to get the Claude launch path.

    Raised by :func:`ensure_harness_matches_claude_launch` — the v4
    step-2 loudness guard. See that function's docstring for the full
    story; the short version is that until the descriptor registry
    (migration step 4) lands, the lifecycle launch path can only start
    the Claude harness, and starting it under a spec that declared a
    different harness is a wrong-vendor launch that must refuse loudly
    instead of proceeding silently.
    """


#: The v4 card tracking harness-aware runtime dispatch (migration step 4,
#: the descriptor registry). Until it lands, sac VALIDATES ``harness:
#: openai`` but cannot LAUNCH it through the lifecycle runtime path.
V4_HARNESS_DISPATCH_CARD = "sac-v4-layering-refactor-harness-runtime-inference-20260813"


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


def _harness_logger():
    """scitex-logging logger, imported lazily.

    Same pattern as ``config.__init__._config_logger``: the
    fleet-consistent coloured ``ERRO:`` stderr line (operator directive
    2026-07-10), imported inside the function so the package's ~300 ms
    first-import auto-configuration never taxes ``import
    scitex_agent_container.config`` itself.
    """
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def ensure_harness_matches_claude_launch(
    config, *, launching: str, log: bool = True, launching_key: str = ""
) -> None:
    """Refuse LOUDLY when ``config.harness`` is non-Anthropic but the
    calling code path is about to launch ``launching`` — a Claude-family
    runner — anyway.

    THE v4 STEP-2 LOUDNESS GUARD (additive; card
    ``sac-v4-layering-refactor-harness-runtime-inference-20260813``).
    Before it, the three launch-path dispatch sites read
    ``getattr(config, "provider", None)`` — a field the harness rename
    REMOVED from ``AgentConfig`` — so every one of those branches was
    dead: a ``harness: openai`` spec got OPENAI_* auth env (the auth
    path reads ``config.harness`` correctly) and then SILENTLY launched
    the Claude runner. A wrong-vendor launch with no error anywhere is
    exactly what the operator's 「エラーが握りつぶされない」 directive
    forbids. Selection stays byte-identical for every Anthropic-harness
    spec; harness-aware DISPATCH is migration step 4 (the descriptor
    registry), deliberately NOT this guard.

    ``kind: AgentProxy`` is exempt: the a2a proxy runner is
    vendor-neutral, so a harness value on a proxy spec mismatches
    nothing this guard protects.

    ``log=False`` skips the scitex-logging ERROR line for callers on
    shared read paths (``_get_runtime`` also serves every status / list
    / health walk — each of which degrades a raise into an UNKNOWN
    verdict that KEEPS this message — and a per-read stderr line would
    contaminate CliRunner-captured ``--json`` output; the same ruling
    that placed ``warn_if_legacy_harness_key`` on the start path). The
    raise itself is never skipped.

    Raises :class:`HarnessRuntimeMismatchError` naming (a) what the spec
    asked for and through which key, (b) what was actually about to
    launch, (c) the caller's ``file:line`` (the decision site), and
    (d) the v4 gap card id.
    """
    harness = (
        str(getattr(config, "harness", "") or DEFAULT_AGENT_HARNESS).strip().lower()
    )
    if harness == DEFAULT_AGENT_HARNESS:
        return
    if getattr(config, "kind", "Agent") == "AgentProxy":
        return
    # 2026-09-05: the first non-Anthropic entry with a full launch path.
    # The caller states WHICH registry entry it is about to launch
    # (``launching_key``); a codex spec headed for the codex TUI is a
    # correct routing, not a wrong-vendor one. Every other non-Anthropic
    # combination still refuses below — the predicate stays "is the
    # declared harness what this path launches?", answered per entry.
    from ._harness_registry import CODEX_TUI

    if harness == "codex" and launching_key == CODEX_TUI:
        return
    caller = sys._getframe(1)
    site = f"{caller.f_code.co_filename}:{caller.f_lineno}"
    key = (
        LEGACY_HARNESS_KEY
        if getattr(config, "harness_key_is_legacy", False)
        else HARNESS_KEY
    )
    message = (
        f"REFUSING to launch agent {getattr(config, 'name', '<unknown>')!r}: "
        f"spec.{key} declares harness={harness!r}, but this code path was "
        f"about to launch {launching} (decided at {site}). Harness-aware "
        f"runtime dispatch is a KNOWN v4 gap — card "
        f"{V4_HARNESS_DISPATCH_CARD} — so until the descriptor registry "
        f"(migration step 4) lands, a {harness!r} spec cannot start through "
        "the lifecycle launch path at all; proceeding would silently run "
        f"the Claude harness under a spec that asked for {harness!r}. "
        "Working alternatives today: drive the OpenAI SDK through "
        "``a2a.handler: openai_session`` (the grant-agent pattern), or set "
        f"``{HARNESS_KEY}: {DEFAULT_AGENT_HARNESS}``."
    )
    if log:
        _harness_logger().error(message)
    raise HarnessRuntimeMismatchError(message)
