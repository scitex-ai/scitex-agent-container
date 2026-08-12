"""The GATE the ``to_home_layers`` sweep must pass: nobody got disarmed.

:mod:`..runtimes._hook_arming_diff` compares two fleet snapshots of what is
armed. It was merged with nothing to feed it and nothing consuming it. This
module is both halves — the collector that builds a snapshot, and the verdict
that decides whether a sweep may stand on one.

**Snapshots are DERIVED, never read back off disk.** ``hook-origins.json`` is
written at deploy time, so on a fleet that has not redeployed since that
landed, zero manifests exist. Reading them would compare nothing against
nothing. Deriving the cascade here — load the spec, resolve its layers, merge,
reshape the provenance — measures the specs as they are RIGHT NOW, which is
the only thing a pre/post-write comparison can honestly be about.

**An agent that could not be measured is never recorded as "no hooks".** The
tempting shortcut is to catch the error and store ``{}``; two such agents then
compare equal and the diff reports them UNCHANGED. That is the exact collapse
of "I could not tell" into "it is fine" that the diff's own ``unmeasured``
field exists to prevent, so an unmeasurable agent is kept OUT of the snapshot
map and named in :attr:`ArmingSnapshot.unmeasurable` instead.

**The gate is not the diff.** ``diff_hook_arming({}, {})`` is ``safe`` — 0
agents compared, nothing moved, deliberately so. Wire a sweep straight to that
and it waves through a migration over a fleet it never looked at, which is not
a hypothetical: an empty comparison is what a collector that silently found
nothing produces. :class:`ArmingGateVerdict` therefore adds a FLOOR — the
comparison must cover the number of agents the plan covers, and that number
must not be zero — and refuses while either side has an unmeasurable agent.
``safe`` on the diff alone is necessary and nowhere near sufficient.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_config
from ..runtimes._hook_arming_diff import HookArmingDiff, diff_hook_arming
from ..runtimes._hook_origin_manifest import hook_origins

# Shared with the planner on purpose: an agent named as unreadable by the plan
# and unmeasurable by the gate must render identically, or a report reads like
# two different agents had two different problems.
from ._layers_migration_plan import _reason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArmingSnapshot:
    """What every measurable agent arms, plus the ones we could NOT measure."""

    #: agent -> {event: {command: layer}}. Only agents actually measured.
    origins: "dict[str, dict[str, dict[str, str]]]" = field(default_factory=dict)
    #: ``"<agent>: <reason>"`` for each agent whose arming could not be derived.
    unmeasurable: "tuple[str, ...]" = ()

    @property
    def measured(self) -> int:
        return len(self.origins)


def agent_arming(config) -> "dict[str, dict[str, str]]":
    """Derive ONE agent's ``{event: {command: layer}}`` from its config.

    The same three calls ``deploy_to_home`` makes, in the same order, so this
    measures the cascade the deploy would actually build rather than a parallel
    re-implementation that could drift from it.
    """
    from ..runtimes._to_home_resolve import settings_layer_dirs
    from ..runtimes._to_home_settings import settings_cascade_provenance

    return hook_origins(settings_cascade_provenance(settings_layer_dirs(config)))


def fleet_arming_snapshot(spec_paths: "list[Path]") -> ArmingSnapshot:
    """Measure hook arming across ``spec_paths``, right now, from the specs.

    Every spec is re-loaded on every call. That is not waste — it is the
    contract: the ``after`` snapshot has to see the bytes the sweep just wrote,
    and a cached parse would report the pre-write cascade and pass the gate on
    a file it never re-read. (``config._spec_cache`` keys on size+mtime_ns and
    the insert changes both, so it misses; this call does not depend on that.)
    """
    origins: dict[str, dict[str, dict[str, str]]] = {}
    unmeasurable: list[str] = []
    for path in spec_paths:
        agent = path.parent.name
        # stx-allow: fallback (reason: an agent we cannot measure must become
        # the THIRD value, not an empty arming map that would compare as
        # "unchanged" against another empty one.)
        try:
            origins[agent] = agent_arming(load_config(path))
        except Exception as exc:
            logger.error("arming snapshot: could not measure %s — %s", path, exc)
            unmeasurable.append(_reason(agent, exc))
    return ArmingSnapshot(origins=origins, unmeasurable=tuple(unmeasurable))


@dataclass(frozen=True)
class ArmingGateVerdict:
    """May the sweep stand? ``safe``/``summary()`` match what the apply reads.

    Deliberately the same two-member surface
    :func:`.._layers_migration_apply.apply_migration` expects of its ``verify``
    callable, so this drops in without the apply learning anything about hooks.
    """

    diff: HookArmingDiff
    #: How many agents the comparison MUST cover — the size of the population
    #: the plan touched. Anything less means agents went unlooked-at.
    expected: int
    before_unmeasurable: "tuple[str, ...]" = ()
    after_unmeasurable: "tuple[str, ...]" = ()

    @property
    def floor_met(self) -> bool:
        """Did the comparison actually cover the whole population?

        ``expected > 0`` is not pedantry. A comparison of zero agents against
        zero agents satisfies every other condition here, and a collector that
        found nothing produces exactly that — so without this clause the gate's
        most likely failure mode reads as its strongest pass.
        """
        return self.expected > 0 and self.diff.agents_compared == self.expected

    @property
    def safe(self) -> bool:
        """True only when the whole population was measured BOTH sides, unchanged."""
        return (
            self.diff.safe
            and self.floor_met
            and not self.before_unmeasurable
            and not self.after_unmeasurable
        )

    def summary(self) -> str:
        """One line. Says which condition failed, never just that one did."""
        parts: list[str] = []
        if not self.diff.safe:
            parts.append(self.diff.summary())
        if not self.floor_met:
            parts.append(
                f"FLOOR NOT MET: {self.diff.agents_compared} agent(s) compared, "
                f"{self.expected} expected — the sweep was not verified over "
                f"the population it touched"
            )
        if self.before_unmeasurable:
            parts.append(
                f"{len(self.before_unmeasurable)} agent(s) UNMEASURABLE before"
            )
        if self.after_unmeasurable:
            parts.append(f"{len(self.after_unmeasurable)} agent(s) UNMEASURABLE after")
        if not parts:
            return (
                f"{self.diff.agents_compared} agent(s) measured both sides: "
                f"hook arming identical"
            )
        return "; ".join(parts)

    def to_dict(self) -> dict:
        """JSON-shaped, with every field present on every call."""
        return {
            "safe": self.safe,
            "floor_met": self.floor_met,
            "expected": self.expected,
            "agents_compared": self.diff.agents_compared,
            "unchanged": list(self.diff.unchanged),
            "lost": {k: list(v) for k, v in self.diff.lost.items()},
            "gained": {k: list(v) for k, v in self.diff.gained.items()},
            "reattributed": {k: list(v) for k, v in self.diff.reattributed.items()},
            "unmeasured": list(self.diff.unmeasured),
            "unexpected": list(self.diff.unexpected),
            "before_unmeasurable": list(self.before_unmeasurable),
            "after_unmeasurable": list(self.after_unmeasurable),
            "summary": self.summary(),
        }


def gate_arming(
    before: ArmingSnapshot, after: ArmingSnapshot, *, expected: int
) -> ArmingGateVerdict:
    """Compare two snapshots against the population the sweep was supposed to cover."""
    return ArmingGateVerdict(
        diff=diff_hook_arming(before.origins, after.origins),
        expected=expected,
        before_unmeasurable=before.unmeasurable,
        after_unmeasurable=after.unmeasurable,
    )


__all__ = [
    "ArmingGateVerdict",
    "ArmingSnapshot",
    "agent_arming",
    "fleet_arming_snapshot",
    "gate_arming",
]
