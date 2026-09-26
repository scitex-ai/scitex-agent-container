"""Fail-closed fleet sizing for Hermes' absolute compaction trigger."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


class HermesFleetBudgetError(ValueError):
    """A trustworthy shared-engine compaction budget cannot be derived."""


@dataclass(frozen=True)
class HermesFleetContextBudget:
    """A deterministic per-agent share of recently observed engine KV capacity."""

    threshold_tokens: int
    usable_capacity_tokens: int
    reserved_capacity_tokens: int
    agent_count: int


def derive_hermes_fleet_context_budget(
    *,
    engine_capacity_tokens: int | None,
    agent_count: int,
    capacity_age_seconds: float | None,
    max_capacity_age_seconds: float = 30.0,
    reserve_ratio: float = 0.25,
) -> HermesFleetContextBudget:
    """Derive a binary-rounded per-agent trigger or refuse untrusted input.

    ``engine_capacity_tokens`` must be an observed shared KV capacity, not a
    model context limit. The observation has an explicit age so callers cannot
    silently reuse capacity across an engine restart or an unreachable probe.
    Rounding each equal share down to a power of two makes deployed thresholds
    stable across insignificant capacity changes and leaves at least the stated
    reserve.
    """
    if type(engine_capacity_tokens) is not int or engine_capacity_tokens <= 0:
        raise HermesFleetBudgetError("engine token capacity is unavailable")
    if type(agent_count) is not int or agent_count <= 0:
        raise HermesFleetBudgetError("agent_count must be a positive integer")
    if (
        type(max_capacity_age_seconds) not in {int, float}
        or not isfinite(max_capacity_age_seconds)
        or max_capacity_age_seconds <= 0
    ):
        raise HermesFleetBudgetError("max_capacity_age_seconds must be positive")
    if (
        type(capacity_age_seconds) not in {int, float}
        or not isfinite(capacity_age_seconds)
        or capacity_age_seconds < 0
        or capacity_age_seconds > max_capacity_age_seconds
    ):
        raise HermesFleetBudgetError("engine token capacity is missing or stale")
    if type(reserve_ratio) not in {int, float} or not 0 < reserve_ratio < 1:
        raise HermesFleetBudgetError("reserve_ratio must be between 0 and 1")

    usable = int(engine_capacity_tokens * (1 - reserve_ratio))
    equal_share = usable // agent_count
    if equal_share < 1:
        raise HermesFleetBudgetError("engine capacity cannot provide one token per agent")
    threshold = 1 << (equal_share.bit_length() - 1)
    reserved = engine_capacity_tokens - threshold * agent_count
    return HermesFleetContextBudget(
        threshold_tokens=threshold,
        usable_capacity_tokens=threshold * agent_count,
        reserved_capacity_tokens=reserved,
        agent_count=agent_count,
    )


__all__ = [
    "HermesFleetBudgetError",
    "HermesFleetContextBudget",
    "derive_hermes_fleet_context_budget",
]
