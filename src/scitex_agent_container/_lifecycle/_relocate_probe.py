"""Turn probe results into :class:`TargetFacts`, where a failed probe stays UNKNOWN.

:mod:`_relocate_preflight` decides; this fills in what it decides about. The
split is the operator's ports-and-adapters point (2026-08-08): sac's core must
not learn how to reach a host, so the checks take facts and this takes
callables. Swap ssh for the listen daemon, or for a test double, and the
decision logic never changes.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE: a probe that FAILS produces
``None`` (not observed), never a falsy value. That distinction is the whole
point of the three-valued preflight, and it is trivially easy to destroy here —
one bare ``except: return False`` in a prober turns "I could not reach the host"
into "the host says no", and the report then refuses (or worse, passes) for a
reason nobody can trace.

It is worth being explicit about which direction each mistake goes:

    probe raises -> False   a missing image reads as present, a busy port as
                            free. The relocation proceeds on fiction.
    probe raises -> None    preflight reports UNKNOWN, refuses, and names the
                            check to re-run. Nothing proceeds on fiction.

So every call here is wrapped, every exception becomes ``None``, and the
exception text is kept on the side so the caller can say WHY a fact is missing
rather than only that it is.

Pure orchestration: no ssh, no subprocess, no imports of anything that talks to
a network. The callables are the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TypeVar

from ._relocate_preflight import TargetFacts

__all__ = ["ProbeOutcome", "TargetProbes", "gather_target_facts", "probe"]

T = TypeVar("T")


@dataclass(frozen=True)
class ProbeOutcome:
    """One probe's result, with the failure kept rather than discarded."""

    value: object | None
    error: str | None = None

    @property
    def observed(self) -> bool:
        return self.error is None


def probe(fn: Callable[[], T]) -> ProbeOutcome:
    """Run ``fn``; on ANY exception record it and return an unobserved outcome.

    Deliberately catches broadly. A prober can fail in as many ways as a network
    can, and enumerating them here would mean the un-enumerated one becomes an
    uncaught crash mid-preflight — which is a worse outcome than an honest
    "could not tell". The exception is preserved, so nothing is actually
    swallowed; it just stops being fatal.
    """
    try:
        return ProbeOutcome(value=fn())
    except Exception as exc:  # stx-allow: fallback (reason: a probe failure must become UNOBSERVED, never a false negative; the exception is preserved on the outcome)
        return ProbeOutcome(value=None, error=f"{type(exc).__name__}: {exc}")


@dataclass
class TargetProbes:
    """The callables that answer each preflight question.

    Every one is optional. An omitted probe leaves its fact ``None`` — which
    preflight reports as UNKNOWN and refuses on, exactly as it would for a probe
    that ran and failed. "Nobody asked" and "asked and could not tell" are the
    same thing from the decision's point of view, and both are distinct from an
    answer.
    """

    reachable: Callable[[], bool] | None = None
    image_present: Callable[[], bool] | None = None
    missing_bind_sources: Callable[[], tuple[str, ...]] | None = None
    card_store_url: Callable[[], str] | None = None
    card_store_reachable: Callable[[], bool] | None = None
    credential_expires_in_s: Callable[[], float] | None = None
    credential_refresh_token_present: Callable[[], bool] | None = None
    supported_runtimes: Callable[[], tuple[str, ...]] | None = None
    rejected_spec_keys: Callable[[], tuple[str, ...]] | None = None
    ports_in_use: Callable[[], tuple[int, ...]] | None = None
    hub_reachable_from_target: Callable[[], bool] | None = None
    sac_on_path: Callable[[], bool] | None = None
    sac_resolved_path: Callable[[], str] | None = None


@dataclass(frozen=True)
class GatherResult:
    """The facts, plus why any of them are missing.

    ``errors`` maps fact name -> failure text. A caller rendering the preflight
    report can then say "credentials: UNKNOWN (SSHTimeout: ...)" instead of the
    bare "not observed" the checks alone can offer.
    """

    facts: TargetFacts
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def all_observed(self) -> bool:
        return not self.errors


_FIELDS: tuple[str, ...] = (
    "reachable",
    "image_present",
    "missing_bind_sources",
    "card_store_url",
    "card_store_reachable",
    "credential_expires_in_s",
    "credential_refresh_token_present",
    "supported_runtimes",
    "rejected_spec_keys",
    "ports_in_use",
    "hub_reachable_from_target",
    "sac_on_path",
    "sac_resolved_path",
)


def gather_target_facts(probes: TargetProbes) -> GatherResult:
    """Run every supplied probe, collecting facts and failures side by side.

    Runs ALL of them rather than stopping at the first failure — the same
    reasoning as preflight returning every check: a dry run that stops early
    makes the operator run it N times to find N problems. A probe that fails
    does not prevent the others from answering.
    """
    values: dict[str, object] = {}
    errors: dict[str, str] = {}
    for name in _FIELDS:
        fn = getattr(probes, name, None)
        if fn is None:
            continue
        outcome = probe(fn)
        if outcome.observed:
            values[name] = outcome.value
        else:
            errors[name] = outcome.error or "unknown failure"
    return GatherResult(facts=TargetFacts(**values), errors=errors)  # type: ignore[arg-type]
