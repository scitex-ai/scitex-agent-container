"""The RESIDENCY axis — ``spec.residency``: does the daemon outlive its work?

v4 migration step 6 (card
``sac-v4-layering-refactor-harness-runtime-inference-20260813``). The
operator's driving requirement, 2026-08-14: an agent should be able to
stay RESIDENT — 「ずっと生きていて、仕事が終わっても待機している」
(keeps living, waits after work is done) — while experiment trials and
one-off workers should exit cleanly when their work completes. Before
this axis that behaviour was implicit per-runner (``--print-stream``
foreground missions, the autonomous drive-until loop); now it is a
DECLARED SPEC FIELD, and the daemon's end is either the declared plan
(ExitRecord reason ``oneshot-complete``) or a contract violation
(``harness-returned`` / ``crashed``) — never an ambiguity.

THE CLOSED SET:

  * ``resident``  (default) — the session daemon parks on ``stop.wait()``
    after every conversation; a turn driver that returns on its own is a
    residency VIOLATION (the step-5 zombie fix ends the daemon loudly).
  * ``one-shot``  — the conversation completing normally IS the plan:
    the daemon exits 0 with ExitRecord reason ``oneshot-complete``.

POSTURE — defaulted, not required-explicit, deliberately. The red-start
ruling (2026-07-21) made every EXISTING field mandatory; ``harness``
could join the required map only because its deprecated ``provider``
spelling already satisfied it across the whole corpus. ``residency`` is
a NEW axis with no legacy spelling: requiring it today would boot-red
every live spec for declaring nothing new — exactly the en-masse
red-start the ``to_home_layers`` precedent avoided (see the EXCLUDED
list in ``_explicit_fields``). The v3→v4 converter materializes the
explicit ``residency:`` line per live spec; turning omission into an
error is that later step. An ILLEGAL value still fails loud at
validation time, naming the closed set.
"""

from __future__ import annotations

from typing import Literal, Mapping

__all__ = [
    "AGENT_RESIDENCIES",
    "AgentResidency",
    "DEFAULT_AGENT_RESIDENCY",
    "ONE_SHOT",
    "RESIDENCY_KEY",
    "RESIDENT",
    "declared_residency",
    "is_known_residency",
    "list_residencies",
    "resolve_spec_residency",
    "residency_coupling_error",
    "residency_value_error",
]

#: The top-level spec key for this axis. No legacy alias — the axis is
#: new; nothing in the corpus ever spelled it differently.
RESIDENCY_KEY = "residency"

AgentResidency = Literal["resident", "one-shot"]

#: The daemon stays alive awaiting more work after a conversation.
RESIDENT: AgentResidency = "resident"

#: The daemon exits cleanly (``oneshot-complete``) when the work is done.
ONE_SHOT: AgentResidency = "one-shot"

#: Matches every live long-running agent today; one-shot is the opt-in.
DEFAULT_AGENT_RESIDENCY: AgentResidency = RESIDENT

#: The closed set. A typo is a validation error naming this set.
AGENT_RESIDENCIES: tuple[str, ...] = (RESIDENT, ONE_SHOT)


def is_known_residency(name: str) -> bool:
    """True when ``name`` is a recognized residency."""
    return name in AGENT_RESIDENCIES


def list_residencies() -> list[str]:
    """The recognized residencies, sorted — for fail-loud error messages."""
    return sorted(AGENT_RESIDENCIES)


def _stated(spec: Mapping, key: str) -> str | None:
    """The STATED value of ``spec[key]``, or ``None`` for "no opinion".

    Same identity rules as the harness axis (``_harness_types._stated``):
    absent, present-with-null, and present-but-empty/whitespace all mean
    "declares nothing". Normalised (stripped + lowercased) — the
    VALUE-legality check owns the casing diagnostic, this function only
    owns identity.
    """
    if not isinstance(spec, Mapping) or key not in spec:
        return None
    raw = spec[key]
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def declared_residency(spec: Mapping) -> str | None:
    """The residency this spec states, or ``None`` when it states none."""
    return _stated(spec, RESIDENCY_KEY)


def resolve_spec_residency(spec: Mapping) -> str:
    """The residency for this spec, defaulting to ``resident``.

    The default is the documented fleet posture (every live long-running
    agent today), not a silent guess — see the module docstring for why
    absence is legal while the required-explicit map exists.
    """
    return declared_residency(spec) or DEFAULT_AGENT_RESIDENCY


def residency_value_error(spec: Mapping) -> list[str]:
    """List-form VALUE check for ``validate_raw``: 0 or 1 message.

    Runs on the WRITTEN value (not the normalised resolution) so the
    error names the exact line the operator has to edit — the same
    posture as the harness value check.
    """
    value = spec.get(RESIDENCY_KEY) if isinstance(spec, Mapping) else None
    if value and not is_known_residency(str(value)):
        return [
            f"spec.{RESIDENCY_KEY} must be one of {list_residencies()} "
            f"(got '{value}'). 'resident' (default) = the session daemon "
            "stays alive awaiting more work after a conversation "
            "completes; 'one-shot' = the daemon exits cleanly (ExitRecord "
            "reason 'oneshot-complete') when its conversation completes."
        ]
    return []


def residency_coupling_error(spec: Mapping, kind: object = None) -> list[str]:
    """List-form COUPLING check: ``one-shot`` needs a session daemon.

    ``one-shot`` is honoured by the shared residency daemon
    (``_runners/session_daemon``), which only runner-hosted harnesses
    have. The interactive TUI is externally hosted — the ``claude``
    binary owns its own loop and sac has no daemon there to end — and
    ``kind: AgentProxy`` is an inherently resident forwarder. Both are
    refused LOUDLY at validation time rather than silently ignored (the
    operator's 「エラーが握りつぶされない」 directive).

    Declines (returns ``[]``) when the harness/runtime axes are
    themselves invalid — those checks own their own diagnostics, and a
    second error derived from an unresolvable pair would only obscure
    the first.
    """
    if declared_residency(spec) != ONE_SHOT:
        return []
    from ._harness_types import V4_HARNESS_DISPATCH_CARD

    if kind == "AgentProxy":
        return [
            f"spec.{RESIDENCY_KEY}: {ONE_SHOT} is not accepted on "
            "kind: AgentProxy — the proxy forwarder is inherently "
            "resident (it serves until stopped, it has no conversation "
            "to complete). Delete the line or write "
            f"'{RESIDENCY_KEY}: {RESIDENT}'."
        ]
    from ._harness_registry import (
        HARNESS_DESCRIPTORS,
        UnmappableHarnessError,
        resolve_harness_key,
    )
    from ._harness_types import HarnessKeyConflictError

    try:
        key = resolve_harness_key(spec)
    except (UnmappableHarnessError, HarnessKeyConflictError):
        return []  # the harness/runtime checks own that diagnostic
    if HARNESS_DESCRIPTORS[key].hosted != "runner":
        return [
            f"spec.{RESIDENCY_KEY}: {ONE_SHOT} needs a runner-hosted "
            "harness (a session daemon to honour the planned exit), but "
            f"this spec resolves to harness entry '{key}', which is "
            "externally hosted — the interactive TUI owns its own loop "
            "and sac has no daemon there to end. Not supported yet (card "
            f"{V4_HARNESS_DISPATCH_CARD}); set 'runtime: "
            "claude-agent-sdk' for a one-shot agent, or drop the line "
            "to stay resident."
        ]
    return []
