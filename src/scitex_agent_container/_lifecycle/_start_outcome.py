"""Start-outcome sentinels — let a caller tell "started" from "no-op".

``agent_start`` has always returned a bare ``True`` from BOTH the branch
that really launched a container and the branch that found the agent
already alive and did nothing. Those two returns are indistinguishable,
and that ambiguity is a bug generator: a RESTART that degrades into the
idempotent no-op is reported to its caller as a successful restart, so a
supervisor counting rc=0 marks an agent as rolled when it is still the
OLD process on its OLD credentials (incident 2026-07-12, scitex-storage —
``sac agents start`` printed "already running ... No-op." immediately
followed by "SUCC: scitex-storage started").

The fix is to keep returning something TRUTHY — a plain ``sac agents
start`` over a live agent is a legitimate success, and downgrading it to
``False`` would invent the mirror-image lie — while attaching WHY.

Why an ``int`` subclass rather than a dataclass / enum
------------------------------------------------------
``agent_start`` is declared ``-> bool`` and its result is consumed by a
long tail of callers (the CLI restart envelope, the MCP tools, the
lifecycle tests) that do ``if result:`` or the deliberate ``result is not
False``. Returning a non-numeric object would break the first form's
intent silently and force a synchronised edit of every call site.

``bool`` itself cannot be subclassed, but ``bool`` IS an ``int``
subclass, so an ``int`` subclass is the idiomatic way to stay
bool-compatible:

  * ``bool(NOOP_ALREADY_RUNNING)`` is ``True`` — ``if result:`` unchanged.
  * ``NOOP_ALREADY_RUNNING is not False`` is ``True`` — the restart CLI's
    deliberate identity check (which exists so a runtime returning
    ``None`` is not read as failure) keeps its meaning.
  * ``NOOP_ALREADY_RUNNING == True`` is ``True`` — equality callers are
    unaffected.

Only a caller that ASKS (via :func:`outcome_kind`) sees the difference,
so this is additive: no existing behaviour changes, and the information
that was previously destroyed is now available to whoever needs it.
"""

from __future__ import annotations

__all__ = [
    "StartOutcome",
    "NOOP_ALREADY_RUNNING",
    "KIND_ALREADY_RUNNING",
    "outcome_kind",
]

#: ``kind`` of the "agent was already alive; nothing was launched" outcome.
KIND_ALREADY_RUNNING = "already-running"


class StartOutcome(int):
    """A bool-compatible ``agent_start`` result that remembers WHY.

    Truthiness is the ``int`` value (1 == success-shaped), so every
    existing ``if result:`` / ``result is not False`` caller behaves
    exactly as it did when this was a bare ``True``. ``kind`` carries the
    detail those callers used to have no way to ask for.
    """

    kind: str

    def __new__(cls, value: int, kind: str) -> "StartOutcome":
        obj = super().__new__(cls, value)
        obj.kind = kind
        return obj

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"StartOutcome({int(self)}, kind={self.kind!r})"


#: Returned by ``agent_start`` when POSITIVE liveness evidence pinned the
#: idempotent no-op branch: the agent was already up and NOTHING was
#: launched. Truthy, because re-starting a running agent is a legitimate
#: success for a plain ``sac agents start``; but a caller whose contract
#: is "the process must have CYCLED" (i.e. a restart) must treat this as a
#: FAILURE — see ``cli_pkg/lifecycle/_restart.py``.
NOOP_ALREADY_RUNNING = StartOutcome(1, KIND_ALREADY_RUNNING)


def outcome_kind(result: object) -> str | None:
    """Return the ``kind`` of a start result, or ``None`` if it has none.

    Safe on the plain ``True`` / ``False`` / ``None`` that the older
    return contract produced (and that hand-rolled test doubles still
    return), so callers can interrogate any start result without first
    proving it came from a version that carries the sentinel.
    """
    return getattr(result, "kind", None)
