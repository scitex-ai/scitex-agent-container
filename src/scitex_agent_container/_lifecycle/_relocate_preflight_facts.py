"""The SHAPES a relocation preflight is written in: observations in, a verdict out.

Split out of :mod:`_relocate_preflight` so that the vocabulary (what can be
observed, and what one answer looks like) is readable without scrolling past
eleven predicates. The predicates live in :mod:`_relocate_checks`; the
orchestration and the public API stay in :mod:`_relocate_preflight`.

THE ONE RULE THESE TYPES ENCODE. Every observable is ``| None`` and defaults to
``None`` meaning NOT OBSERVED, which is deliberately distinct from an observed
negative. A prober that could not answer must say so rather than reporting a
falsy default, because a default that reads as "absent" turns a failed probe
into a confident wrong answer — and an unknown folded into a pass is exactly how
the 2026-08-07 move reported healthy while doing nothing.

Pure data. No I/O, no clock, no decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._relocate_origin import RepoWork

__all__ = [
    "Check",
    "PreflightReport",
    "SourceFacts",
    "TargetFacts",
]


@dataclass(frozen=True)
class Check:
    """One preflight answer.

    ``ok`` is three-valued: ``True`` pass, ``False`` fail, ``None`` COULD NOT
    DETERMINE. ``hint`` is required whenever the answer is not a pass — an error
    that only says what broke is half-written; it must also say what to do.
    """

    name: str
    ok: bool | None
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Check.name must be non-empty")
        if self.ok not in (True, False, None):
            raise ValueError(f"Check.ok must be True/False/None, got {self.ok!r}")
        if not self.detail:
            raise ValueError(f"Check {self.name!r}: detail must be non-empty")
        if self.ok is not True and not self.hint:
            raise ValueError(
                f"Check {self.name!r}: a non-passing check must carry a hint saying what to do about it"
            )


@dataclass(frozen=True)
class TargetFacts:
    """What was OBSERVED about the target host. Gathering happens elsewhere.

    Every field is ``| None`` and defaults to ``None`` meaning NOT OBSERVED —
    distinct from an observed negative. A prober that could not answer must say
    so rather than reporting a falsy default, because a default that reads as
    "absent" turns a failed probe into a confident wrong answer.
    """

    reachable: bool | None = None
    image_present: bool | None = None
    #: Bind SOURCE paths that do not exist on the target (e.g. /mnt/c on nas).
    missing_bind_sources: tuple[str, ...] | None = None
    card_store_url: str | None = None
    card_store_reachable: bool | None = None
    #: Seconds until the target-local credential expires. Negative = expired.
    credential_expires_in_s: float | None = None
    credential_refresh_token_present: bool | None = None
    supported_runtimes: tuple[str, ...] | None = None
    #: Top-level spec keys the target's validator rejects (e.g. "provider").
    rejected_spec_keys: tuple[str, ...] | None = None
    ports_in_use: tuple[int, ...] | None = None
    hub_reachable_from_target: bool | None = None
    #: Whether ``command -v sac`` finds it in the NON-INTERACTIVE ssh PATH —
    #: which is the PATH every remote sac call actually runs under.
    sac_on_path: bool | None = None
    #: Where sac was found by looking directly (login shell, known venvs).
    #: ``""`` means LOOKED AND FOUND NOTHING; ``None`` means nobody looked. The
    #: distinction is the whole check: not-on-PATH plus found-at-a-path is a
    #: PATH problem, not-on-PATH plus found-nowhere is a missing install.
    sac_resolved_path: str | None = None


@dataclass(frozen=True)
class SourceFacts:
    """What was observed about the host being LEFT.

    A separate type rather than more fields on :class:`TargetFacts`, because the
    two are gathered by different means — the target's facts come back over ssh,
    the source's are read locally — and merging them would make it possible to
    fill a source field from a target probe without anything noticing.
    """

    #: Every repo the agent works in, with its un-saved counts. ``None`` means
    #: no scan ran; an empty tuple means a scan ran and found no repos.
    repos: tuple[RepoWork, ...] | None = None


@dataclass(frozen=True)
class PreflightReport:
    """The whole answer, in one shape.

    ``ok`` is three-valued and derived, never asserted independently of the
    checks: True only when every check passed, None when nothing failed but
    something is unknown, False when anything failed. Unknowns are reported
    SEPARATELY from failures so a reader can tell "this is wrong" from "I could
    not tell" — and neither reads as go.
    """

    agent: str
    to_host: str
    checks: tuple[Check, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("PreflightReport.agent must be non-empty")
        if not self.to_host:
            raise ValueError("PreflightReport.to_host must be non-empty")

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.ok is False)

    @property
    def unknown(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.ok is None)

    @property
    def ok(self) -> bool | None:
        if self.failed:
            return False
        if self.unknown:
            return None
        return True

    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this relocation must not proceed, each with its hint.

        Failures first, then unknowns. Empty only when the answer is a clean go.
        """
        return tuple(
            f"{c.name}: {c.detail} -> {c.hint}" for c in (self.failed + self.unknown)
        )
