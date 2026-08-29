"""The harness registry — ONE table describing every way sac runs an agent loop.

v4 migration step 4 (card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``, design
comment 2026-08-14). Before this module, the answer to "which harness
does this spec select, and what does that imply?" was smeared across six
hardcoded sets that had to agree by hand:

  * ``config/_validation._VALID_RUNTIMES`` (the accepted spellings),
  * ``_lifecycle/_runtime_select._get_runtime`` (spelling → adapter),
  * the two inner-argv dispatches
    (``runtimes/_apptainer_inner_argv.build_inner_argv`` and the
    pre-built-argv module choice in
    ``runtimes/_apptainer_build_argv.build_run_argv``),
  * ``_lifecycle/_sdk_heartbeat_loop._TUI_RUNTIMES`` (who beats whom),
  * the harness-axis enums (``config/_harness_types.AGENT_HARNESSES``,
    ``runtimes/_apptainer_provider._VALID_AGENT_HARNESSES``).

Every one of those now DERIVES from :data:`HARNESS_DESCRIPTORS`, so a
fourth harness is one dict entry here, not a six-file scavenger hunt.

TWO HALF-WIRED SPEC AXES, ONE KEY. Today's specs reach the harness
through two fields that each carry half the answer: ``spec.runtime`` is
the LAUNCH MODE (``tui`` vs headless SDK runner — an Anthropic-family
distinction wearing a vendor-neutral name) and ``spec.harness`` is the
SDK FAMILY (``anthropic`` | ``openai``). :func:`resolve_harness_key`
collapses the pair into one registry key at config-read time — NO spec
on disk is edited; the axes stay as they are until the v4 spec schema
lands. The mapping is TOTAL over the accepted spellings and an
unmappable combination raises :class:`UnmappableHarnessError` naming
both spec values and the card (the operator's errors-reach-the-caller
directive).

WHAT THIS STEP DOES NOT CHANGE (behavior-preserving by contract):

  * The step-2 refusal (PR #1039) still owns wrong-vendor protection:
    a ``harness: openai`` spec resolves to a real registry key here, but
    the lifecycle launch path still cannot START it —
    ``ensure_harness_matches_claude_launch`` refuses before any dispatch
    site consults a descriptor. Step 7 moved the openai RUNNER onto the
    shared session daemon; key-based LAUNCH of non-Anthropic harnesses
    stays behind that refusal until the canary step proves the runner.
  * The ``SAC_PROVIDER`` ops-only env override keeps its own surface
    (``runtimes/_apptainer_provider.resolve_agent_harness``); this
    resolver reads the SPEC axes only.
  * ``kind: AgentProxy`` is not a harness — the proxy runner is
    vendor-neutral and dispatched by ``kind``, outside this registry.

THE REGISTRY IS CODE, NOT CONFIG: entries live in this module, are
frozen dataclasses, and are import-time-validated (no YAML surface —
deliberate for this step). Vendor names appear ONLY inside the entries
(and the private constants feeding them); the machinery — the
dataclass, the resolver, the derivation helpers — is vendor-neutral and
named by intent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:  # pragma: no cover — typing only, no runtime import cycle
    # ``Path`` moved out with the per-entry callables (the only things that
    # annotated a state_dir) — it now lives in ``_harness_callables``.
    from ._types import AgentConfig

__all__ = [
    "CLAUDE_AGENT_SDK",
    "CLAUDE_CODE_TUI",
    "CODEX_SDK",
    "HARNESS_DESCRIPTORS",
    "HarnessDescriptor",
    "OPENAI_AGENTS",
    "UnmappableHarnessError",
    "host_probed_runtime_spellings",
    "known_harnesses",
    "resolve_harness_key",
    "runtime_spellings_for",
    "valid_runtime_spellings",
]


# ---------------------------------------------------------------------------
# Registry keys — one per harness sac can run TODAY. These are the values
# :func:`resolve_harness_key` returns and the only strings a consumer
# should branch on (never the raw spec spellings).
# ---------------------------------------------------------------------------

#: The interactive Claude Code TUI in a tmux PTY (``spec.runtime: tui`` /
#: unset — the operator-directive-2026-06-15 default).
CLAUDE_CODE_TUI = "claude-code-tui"

#: The headless ``claude-agent-sdk`` session runner
#: (``spec.runtime: claude-agent-sdk``; legacy ``apptainer`` maps here).
CLAUDE_AGENT_SDK = "claude-agent-sdk"

#: The ``openai-agents`` SDK session runner (``spec.harness: openai``).
OPENAI_AGENTS = "openai-agents"

#: The ``openai-codex`` Python SDK session runner (``spec.harness: codex``).
#:
#: NOT the same thing as ``spec.claude.provider: codex`` — that value is an
#: INFERENCE backend (``config._provider_registry``: the local scitex-genai
#: gateway at 127.0.0.1:18765 translating Anthropic Messages to a ChatGPT
#: Codex subscription, with Claude Code still running the loop). THIS key is
#: a HARNESS: the ``codex`` agent program runs the loop and brings its own
#: file-edit / exec / apply_patch tooling. The two axes are the ones this
#: package split apart in #1027 / ``config._harness_types``, and ``codex``
#: is the first value to appear on BOTH — so the composition is refused
#: loudly rather than silently resolved (see
#: ``runtimes._apptainer_codex_env.codex_env_flags``).
CODEX_SDK = "codex-sdk"


class UnmappableHarnessError(ValueError):
    """``spec.harness`` + ``spec.runtime`` select no registered harness.

    A ``ValueError`` subclass so every caller that historically caught
    ``_get_runtime``'s ``ValueError("Unsupported runtime: ...")`` keeps
    working unchanged. The message always names BOTH spec values and the
    v4 card (errors-reach-the-caller directive, operator 2026-08-14).
    """


def _v4_card() -> str:
    # Lazy: _harness_types imports THIS module at module level (to derive
    # AGENT_HARNESSES), so the reverse import must stay call-time only.
    from ._harness_types import V4_HARNESS_DISPATCH_CARD

    return V4_HARNESS_DISPATCH_CARD


# ---------------------------------------------------------------------------
# Per-entry callables live in ``_harness_callables`` — the split the
# 512-line cap forced when the FOURTH harness landed (see that module's
# docstring). Imported by name so an unused hook lints as an error.
# The runner-module constants keep their private spellings here because
# ``runtimes/_apptainer_inner_argv`` and ``_apptainer_build_argv`` derive
# their ``RUNNER_MODULE*`` re-exports from the DESCRIPTORS, not from these.
# ---------------------------------------------------------------------------

from ._harness_callables import (  # noqa: F401 (re-export)
    CLAUDE_SESSION_RUNNER as _CLAUDE_SESSION_RUNNER,
    CODEX_SESSION_RUNNER as _CODEX_SESSION_RUNNER,
    OPENAI_SESSION_RUNNER as _OPENAI_SESSION_RUNNER,
    _claude_env_and_binds,
    _claude_sdk_inner_argv,
    _claude_tui_inner_argv,
    _codex_env_and_binds,
    _codex_sdk_inner_argv,
    _noop_prepare_home,
    _openai_agents_inner_argv,
    _openai_env_and_binds,
)


@dataclass(frozen=True)
class HarnessDescriptor:
    """Everything sac needs to know about ONE harness, in one row.

    The six BEHAVIORAL fields are the v4 design's descriptor contract
    (card comment 2026-08-14): ``inner_argv`` / ``hosted`` /
    ``beat_writer`` / ``can_resume`` / ``env_and_binds`` /
    ``prepare_home``. The remaining fields are the registry's own data:
    ``key`` (identity) and the two SELECTION fields (``spec_harness``,
    ``spec_runtimes``) that make the spec→key mapping and the derived
    validation sets live IN the entry — which is exactly what lets a
    fourth harness be one dict entry instead of six edits.
    """

    #: Registry identity — what :func:`resolve_harness_key` returns.
    key: str

    #: The ``spec.harness`` family this entry serves (selection data).
    spec_harness: str

    #: The ``spec.runtime`` spellings that select this entry WITHIN its
    #: family (selection data). Empty when the family has one entry and
    #: the runtime axis does not discriminate.
    spec_runtimes: frozenset[str]

    #: The ``python -m`` runner module, or ``None`` for an external
    #: process (the interactive ``claude`` binary is not a sac runner).
    runner_module: str | None

    #: ``inner_argv(config, options) -> list[str]`` — the harness's inner
    #: process argv, BEFORE the uniform container-shell wrap (git-env
    #: alias + startup_commands + ``exec``) that ``build_inner_argv``
    #: applies to every entry identically.
    inner_argv: Callable[..., list[str]]

    #: Who owns the process loop: ``"runner"`` = a sac session runner
    #: hosts it (daemon residency, pid file, a2a sidecar);
    #: ``"external"`` = an external binary owns its own loop and sac
    #: supervises from outside (tmux pane).
    hosted: Literal["runner", "external"]

    #: Who writes liveness beats: ``"in-process"`` = the runner stamps
    #: its own heartbeat; ``"host-probe"`` = a host-side loop probes and
    #: stamps (an external process cannot beat for itself).
    beat_writer: Literal["in-process", "host-probe"]

    #: Whether the harness can resume a prior conversation/session.
    can_resume: bool

    #: ``env_and_binds(config, state_dir) -> list[str]`` — the harness's
    #: auth/backend ``--env`` / ``--bind`` container flags.
    env_and_binds: Callable[..., list[str]]

    #: ``prepare_home(config) -> None`` — per-harness home extras beyond
    #: the shared ``to_home`` machinery. Default no-op (per the design).
    prepare_home: Callable[..., None] = field(default=_noop_prepare_home)


#: THE registry. One entry per harness; a fourth harness is one more row.
HARNESS_DESCRIPTORS: dict[str, HarnessDescriptor] = {
    descriptor.key: descriptor
    for descriptor in (
        HarnessDescriptor(
            key=CLAUDE_CODE_TUI,
            spec_harness="anthropic",
            spec_runtimes=frozenset({"", "tui"}),
            runner_module=None,  # inner process is the `claude` binary
            inner_argv=_claude_tui_inner_argv,
            hosted="external",
            beat_writer="host-probe",  # pane-activity epoch, host-stamped
            can_resume=True,  # tmux-resume / --continue conversation walk
            env_and_binds=_claude_env_and_binds,
        ),
        HarnessDescriptor(
            key=CLAUDE_AGENT_SDK,
            spec_harness="anthropic",
            # "apptainer" is the pre-2026-06-13 container-engine spelling,
            # honoured as a back-compat alias of the SDK runner (see
            # _runtime_select.warn_if_legacy_apptainer_runtime).
            spec_runtimes=frozenset({"apptainer", "claude-agent-sdk"}),
            runner_module=_CLAUDE_SESSION_RUNNER,
            inner_argv=_claude_sdk_inner_argv,
            hosted="runner",
            beat_writer="in-process",  # heartbeat.json, stamped by the runner
            can_resume=True,  # persisted session_id + history-walk recovery
            env_and_binds=_claude_env_and_binds,
        ),
        HarnessDescriptor(
            key=OPENAI_AGENTS,
            spec_harness="openai",
            # Sole entry of its family — the harness axis alone selects it
            # (the runtime axis names Anthropic launch modes; #1039's
            # refusal keeps those paths from ever launching this entry).
            spec_runtimes=frozenset(),
            runner_module=_OPENAI_SESSION_RUNNER,
            inner_argv=_openai_agents_inner_argv,
            hosted="runner",  # shared session daemon since v4 step 7
            beat_writer="in-process",  # daemon + turn-driver beats, self-stamped
            # The conversation store persists turns under the agent's own
            # name, but sac's resume contract (rehydrate a PRIOR
            # conversation from a caller-supplied session id) is not
            # implemented for this harness — the runner CLI and turn
            # driver both REFUSE --resume-session-id, reading this field.
            can_resume=False,
            env_and_binds=_openai_env_and_binds,
        ),
        HarnessDescriptor(
            key=CODEX_SDK,
            spec_harness="codex",
            # Sole entry of its family, like openai-agents: the runtime
            # axis spells ANTHROPIC launch modes ("tui" / the legacy
            # container-engine values), so it cannot discriminate here.
            spec_runtimes=frozenset(),
            runner_module=_CODEX_SESSION_RUNNER,
            inner_argv=_codex_sdk_inner_argv,
            # RUNNER, not "external" — and the distinction is subtler
            # here than for the other three, so state the evidence.
            # ``openai-codex`` is NOT an in-process API client: its
            # ``CodexClient`` runs ``subprocess.Popen([codex_bin,
            # "app-server", "--listen", "stdio://"])`` and speaks
            # JSON-RPC over that pipe (sdk/python/src/openai_codex/
            # client.py, v0.144.4). But this axis asks WHO OWNS THE
            # SAC-VISIBLE LOOP, not whether a vendor subprocess exists —
            # claude-agent-sdk also spawns the ``claude`` binary and is
            # "runner". The sac inner process here is OUR session runner
            # (``python -m ..._runners.codex_session``) and the codex
            # app-server is its CHILD, so the daemon owns residency, the
            # pid file and the a2a sidecar exactly as for the other two
            # runner entries. "external" is reserved for claude-code-tui,
            # where sac starts NO runner at all and the inner container
            # process IS the vendor binary in a tmux pane.
            hosted="runner",
            beat_writer="in-process",  # daemon + turn-driver beats, self-stamped
            # TRUE — and unlike every prior runner-hosted entry this is a
            # REAL resume, not a hopeful one: the SDK exposes
            # ``AsyncCodex.thread_resume(thread_id: str, ...) ->
            # AsyncThread`` (signature read off the installed 0.144.4
            # wheel), and ``AsyncThread.id`` is the id to persist. The
            # runner reports it as ``RunResult.session_id`` and feeds
            # ``--resume-session-id`` straight back into thread_resume,
            # so this is the FIRST descriptor whose can_resume=True
            # exercises the accept side of the registry-derived gate.
            can_resume=True,
            env_and_binds=_codex_env_and_binds,
        ),
    )
}


# ---------------------------------------------------------------------------
# Resolution — the ONE mapping from spec axes to a registry key.
# ---------------------------------------------------------------------------


def resolve_harness_key(spec: "Mapping | AgentConfig") -> str:
    """Collapse ``spec.harness`` + ``spec.runtime`` into ONE registry key.

    Accepts either a RAW spec mapping (the YAML ``spec:`` block — the
    deprecated ``provider:`` alias and the stated-conflict rule are
    honoured via :func:`config._harness_types.resolve_spec_harness`) or a
    loaded :class:`AgentConfig` (whose ``harness`` the loader already
    resolved). No spec on disk is read or edited — this is a pure
    function of the values handed to it.

    Total over the accepted spellings: every currently-valid
    harness × runtime combination maps to a key. Raises
    :class:`UnmappableHarnessError` (a ``ValueError``) for anything else,
    naming both spec values and the v4 card. Raises
    :class:`config._harness_types.HarnessKeyConflictError` when a raw
    mapping states ``harness`` and ``provider`` disagreeing — same as
    the loader.

    Deliberately blind to ``kind`` (``AgentProxy`` is not a harness) and
    to the ``SAC_PROVIDER`` ops env override (a separate launch-time
    surface — see ``runtimes/_apptainer_provider.resolve_agent_harness``).
    """
    if isinstance(spec, Mapping):
        from ._harness_types import resolve_spec_harness

        harness = resolve_spec_harness(spec)
        runtime = str(spec.get("runtime") or "")
    else:
        from ._harness_types import DEFAULT_AGENT_HARNESS

        harness = (
            str(getattr(spec, "harness", "") or DEFAULT_AGENT_HARNESS)
            .strip()
            .lower()
        )
        runtime = str(getattr(spec, "runtime", "") or "")

    family = [
        descriptor
        for descriptor in HARNESS_DESCRIPTORS.values()
        if descriptor.spec_harness == harness
    ]
    if not family:
        raise UnmappableHarnessError(
            f"Unknown harness: spec.harness={harness!r} (with "
            f"spec.runtime={runtime!r}) matches no registered harness "
            f"family. Known harnesses: {', '.join(known_harnesses())}. "
            f"(v4 harness registry — card {_v4_card()})"
        )
    if len(family) == 1:
        return family[0].key
    for descriptor in family:
        if runtime in descriptor.spec_runtimes:
            return descriptor.key
    mappings = "; ".join(
        f"{sorted(d.spec_runtimes)} → {d.key!r}" for d in family
    )
    raise UnmappableHarnessError(
        f"Unsupported runtime: spec.runtime={runtime!r} maps to no "
        f"registered harness under spec.harness={harness!r}. Accepted "
        f"spec.runtime spellings for this harness: {mappings}. "
        f"(v4 harness registry — card {_v4_card()})"
    )


# ---------------------------------------------------------------------------
# Derivation helpers — the ONLY source for the formerly-hardcoded sets.
# ---------------------------------------------------------------------------


def valid_runtime_spellings() -> frozenset[str]:
    """Every accepted ``spec.runtime`` spelling, derived from the entries.

    The single source for ``config/_validation._VALID_RUNTIMES``.
    """
    spellings: set[str] = set()
    for descriptor in HARNESS_DESCRIPTORS.values():
        spellings |= descriptor.spec_runtimes
    return frozenset(spellings)


def runtime_spellings_for(key: str) -> frozenset[str]:
    """The ``spec.runtime`` spellings that select the entry at ``key``."""
    return HARNESS_DESCRIPTORS[key].spec_runtimes


def host_probed_runtime_spellings() -> frozenset[str]:
    """Spellings of every entry whose beats are HOST-probed, not in-process.

    The single source for ``_lifecycle/_sdk_heartbeat_loop._TUI_RUNTIMES``
    (the set that loop must SKIP so it never clobbers a host-stamped
    pane-activity epoch with wall clock).
    """
    spellings: set[str] = set()
    for descriptor in HARNESS_DESCRIPTORS.values():
        if descriptor.beat_writer == "host-probe":
            spellings |= descriptor.spec_runtimes
    return frozenset(spellings)


def known_harnesses() -> tuple[str, ...]:
    """Every ``spec.harness`` family a registered entry serves, sorted.

    The single source for ``config/_harness_types.AGENT_HARNESSES`` and
    ``runtimes/_apptainer_provider._VALID_AGENT_HARNESSES``.
    """
    return tuple(
        sorted({descriptor.spec_harness for descriptor in HARNESS_DESCRIPTORS.values()})
    )


def _check_registry() -> None:
    """Import-time invariants — a malformed registry fails LOUD at import.

    Guards the two properties resolution depends on: dict key == entry
    key (a copy-paste drift would make lookups lie), and no two entries
    of one family claiming the same ``spec.runtime`` spelling (first-
    match would silently win).
    """
    claimed: dict[tuple[str, str], str] = {}
    for key, descriptor in HARNESS_DESCRIPTORS.items():
        if key != descriptor.key:
            raise RuntimeError(
                f"harness registry corrupt: dict key {key!r} != entry key "
                f"{descriptor.key!r}. Fix the HARNESS_DESCRIPTORS literal."
            )
        for spelling in descriptor.spec_runtimes:
            owner = claimed.setdefault((descriptor.spec_harness, spelling), key)
            if owner != key:
                raise RuntimeError(
                    f"harness registry corrupt: spec.runtime={spelling!r} "
                    f"under spec.harness={descriptor.spec_harness!r} is "
                    f"claimed by both {owner!r} and {key!r} — resolution "
                    "would silently pick the first."
                )


_check_registry()
