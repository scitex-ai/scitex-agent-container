"""Compare two fleet-wide snapshots of WHICH hooks are armed, and by which layer.

Any change to how the ``to_home`` cascade resolves — a new declaration, a
collapsed duplicate layer, a migrated spec — can silently disarm an agent. A
guardless agent looks exactly like a healthy one, so "it deployed fine" proves
nothing. What proves something is: every agent armed the same hooks before and
after, attributed to the same layers.

Reading YAML cannot answer that. The hook-origin manifest can
(:mod:`._hook_origin_manifest`), so this module diffs two of those and returns
one fixed shape.

THE ANSWER IS THREE-VALUED, and the third value is the point. An agent present
in ``before`` but missing from ``after`` has NOT been shown unchanged and has
NOT been shown broken — it was not measured. Folding that into either pole is
how a migration reports success over agents it never looked at, so it gets its
own field (:attr:`HookArmingDiff.unmeasured`) and it makes :attr:`safe` false.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A snapshot is {agent: {event: {command: layer}}} — exactly the shape
# ``_hook_origin_manifest.hook_origins`` returns, keyed by agent.
Snapshot = "dict[str, dict[str, dict[str, str]]]"


@dataclass(frozen=True)
class HookArmingDiff:
    """What changed between two fleet hook-arming snapshots.

    Every field is populated on every comparison — a caller never has to guess
    which key exists on this call. ``safe`` is the single question a migration
    asks, and it is deliberately conservative: anything other than "measured
    both sides, nothing moved" is not safe.
    """

    agents_compared: int = 0
    unchanged: "tuple[str, ...]" = ()
    #: agent -> "<event>: <command>" armed BEFORE and gone AFTER. The dangerous
    #: direction: a guard that stopped being armed.
    lost: "dict[str, list[str]]" = field(default_factory=dict)
    #: agent -> newly armed. Not dangerous, but not free either — an unintended
    #: gain means the cascade grew a source nobody declared.
    gained: "dict[str, list[str]]" = field(default_factory=dict)
    #: agent -> still armed, but now credited to a DIFFERENT layer. The hook
    #: still runs, so this is not a safety regression; it does mean the origin
    #: story changed, which is worth a human look before it is normalised.
    reattributed: "dict[str, list[str]]" = field(default_factory=dict)
    #: In ``before``, absent from ``after``. NOT unchanged, NOT broken —
    #: unmeasured. See the module docstring.
    unmeasured: "tuple[str, ...]" = ()
    #: In ``after``, absent from ``before``. A new agent, or a snapshot taken
    #: over a different set. Also unmeasured, in the other direction.
    unexpected: "tuple[str, ...]" = ()

    def __post_init__(self) -> None:
        """Every compared agent must land in exactly one outcome bucket.

        The arithmetic matters: an agent is `unchanged`, or it appears in at
        least one of lost/gained/reattributed — never neither. If the two sides
        disagree, this diff is under-reporting, and a migration would read a
        short list as "nothing moved". Failing where the object is BUILT beats
        discovering it three layers downstream, in a caller that already
        decided to proceed.

        `unmeasured` / `unexpected` are deliberately NOT in this sum — they are
        agents that were never compared, which is why they are separate fields.
        """
        changed = set(self.lost) | set(self.gained) | set(self.reattributed)
        accounted = len(self.unchanged) + len(changed)
        if accounted != self.agents_compared:
            raise ValueError(
                f"HookArmingDiff is inconsistent: agents_compared="
                f"{self.agents_compared} but {accounted} accounted for "
                f"({len(self.unchanged)} unchanged + {len(changed)} changed). "
                f"An agent must land in exactly one bucket; a mismatch means "
                f"the diff is under-reporting and must not be trusted."
            )
        overlap = set(self.unmeasured) & set(self.unexpected)
        if overlap:
            raise ValueError(
                f"HookArmingDiff is inconsistent: {sorted(overlap)!r} is both "
                f"unmeasured (missing after) and unexpected (missing before), "
                f"which is impossible."
            )

    @property
    def safe(self) -> bool:
        """True only when BOTH sides were measured and nothing moved.

        A migration may proceed on this and nothing weaker. ``gained`` and
        ``reattributed`` count against it too: the promise being verified is
        "identical", not "no worse".
        """
        return not (
            self.lost
            or self.gained
            or self.reattributed
            or self.unmeasured
            or self.unexpected
        )

    def summary(self) -> str:
        """One human-readable line, for a migration log or a refusal message."""
        if self.safe:
            return f"{self.agents_compared} agent(s): hook arming identical"
        parts = []
        if self.lost:
            parts.append(f"{len(self.lost)} agent(s) LOST hooks")
        if self.gained:
            parts.append(f"{len(self.gained)} gained")
        if self.reattributed:
            parts.append(f"{len(self.reattributed)} reattributed")
        if self.unmeasured:
            parts.append(f"{len(self.unmeasured)} UNMEASURED (missing after)")
        if self.unexpected:
            parts.append(f"{len(self.unexpected)} unexpected (missing before)")
        return f"{self.agents_compared} agent(s) compared: " + ", ".join(parts)


def _flatten(origins: "dict[str, dict[str, str]]") -> "dict[str, str]":
    """``{event: {command: layer}}`` -> ``{"event: command": layer}``.

    Flattening on (event, command) rather than command alone keeps the same
    command armed on two events distinguishable — they are two separate
    armings and losing one is a real loss.
    """
    flat: dict[str, str] = {}
    for event, commands in (origins or {}).items():
        for command, layer in (commands or {}).items():
            flat[f"{event}: {command}"] = layer
    return flat


def diff_hook_arming(before: dict, after: dict) -> HookArmingDiff:
    """Compare two ``{agent: {event: {command: layer}}}`` snapshots."""
    before_agents = set(before or {})
    after_agents = set(after or {})
    both = sorted(before_agents & after_agents)

    unchanged: list[str] = []
    lost: dict[str, list[str]] = {}
    gained: dict[str, list[str]] = {}
    reattributed: dict[str, list[str]] = {}

    for agent in both:
        b, a = _flatten(before[agent]), _flatten(after[agent])
        gone = sorted(set(b) - set(a))
        new = sorted(set(a) - set(b))
        moved = sorted(k for k in set(b) & set(a) if b[k] != a[k])
        if gone:
            lost[agent] = gone
        if new:
            gained[agent] = new
        if moved:
            reattributed[agent] = [f"{k} ({b[k]} -> {a[k]})" for k in moved]
        if not (gone or new or moved):
            unchanged.append(agent)

    return HookArmingDiff(
        agents_compared=len(both),
        unchanged=tuple(unchanged),
        lost=lost,
        gained=gained,
        reattributed=reattributed,
        unmeasured=tuple(sorted(before_agents - after_agents)),
        unexpected=tuple(sorted(after_agents - before_agents)),
    )


__all__ = ["HookArmingDiff", "diff_hook_arming"]
