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
    "LeaseFacts",
    "PreflightReport",
    "SourceFacts",
    "SpecSourceDrift",
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
class SpecSourceDrift:
    """The target's answer to "would MY OWN ``sac agents start`` accept this agent".

    Not a re-implementation of the guard — the STATE here is what the target's
    own :func:`.._drift._local.check_spec_source_drift` returned when asked on
    that machine, because the code that will refuse the boot is the only thing
    entitled to say whether it would. ``state`` carries a
    :class:`.._drift._status.DriftState` VALUE (``"behind"``, ``"current"``, …)
    rather than the enum, so the shape survives the ssh round trip as text and
    an unfamiliar word from an older sac stays readable instead of raising.

    ``dirty`` is evidence, NOT part of the verdict: the guard counts commits and
    nothing else, so a dirty tree never refuses a start. It is carried because
    the remedy the guard prints is ``git pull --ff-only``, and on the host this
    was measured on that command aborts — 2389 modified files in the dotfiles
    checkout on scitex-compute-04. A hint naming a command that will not run is
    the same defect as a hint naming a setting that does nothing.
    """

    state: str
    behind: int = 0
    ahead: int = 0
    repo: str = ""
    upstream: str = ""
    #: Files ``git status --porcelain`` reports in the spec-source repo, or
    #: ``None`` when the count was not taken.
    dirty: int | None = None

    def __post_init__(self) -> None:
        if not self.state:
            raise ValueError(
                "SpecSourceDrift.state must be non-empty — a drift answer with no "
                "verdict is not one the start command could have given"
            )


@dataclass(frozen=True)
class LeaseFacts:
    """What the coordinator read out of the LEASE STORE, and about the row's holder.

    A separate type from :class:`TargetFacts` and :class:`SourceFacts` because it
    is neither: the lease is read from the coordinator's own state db, and the
    liveness observation it carries is taken on whichever host that row happens
    to name — a third machine, in the general case.

    ``read`` IS THE OBSERVED FLAG AND ``lease`` IS THE ANSWER. They are separate
    fields because ``lease=None`` is a real and common answer ("this store has
    never held a row for this agent", which bootstraps) and it must not be
    confused with "nobody opened the store", which refuses. One nullable field
    cannot carry both without one of them silently becoming the other.
    """

    read: bool = False
    #: The stored :class:`.._relocate_lease.Lease`, or ``None`` for no row.
    lease: object | None = None
    #: Whether the agent is running ON THE HOST THE ROW NAMES. ``None`` means
    #: nobody looked — which is the honest answer whenever the row names the
    #: source itself, because then no third host needs observing at all.
    recorded_holder_running: bool | None = None
    #: What that observation actually saw, for the report to quote.
    recorded_holder_evidence: str = ""
    #: WHICH store was read. A lease answer is worth exactly as much as the db it
    #: came from, and this fleet has one db per host with no sync between them.
    store: str = ""
    #: The moment the store was read. Required to judge expiry; ``None`` means no
    #: clock was supplied and the expiry question therefore has no answer.
    now: float | None = None


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
    #: Where ``command -v sac`` resolves under THE PATH THIS RELOCATION'S OWN
    #: COMMANDS RUN UNDER — the raw ssh PATH plus the peer's ``env_preamble``,
    #: which :class:`.._relocate_shell.Shell` prepends to every script it sends.
    #: ``""`` means looked-and-found-nothing; ``None`` means nobody looked.
    #:
    #: This is the fact the check should have been reading all along.
    #: ``sac_on_path`` deliberately measures the BARE ssh PATH, which is a
    #: stricter question than the relocation needs to answer, so a host whose
    #: preamble already puts sac on PATH failed a check about a PATH nothing
    #: here uses.
    sac_usable_path: str | None = None
    #: Whether an ``env_preamble`` was declared for this peer AND applied to the
    #: probe. Observed by construction rather than measured on the target: the
    #: prober knows what it sent. It qualifies the two facts above — "sac is not
    #: reachable" means something different when a preamble is already in play —
    #: and it is what keeps the failure hint from recommending a setting that is
    #: already set.
    preamble_declared: bool | None = None
    #: What the TARGET'S OWN sac says about the spec source it would launch from.
    #: ``None`` means it was not asked or could not answer.
    spec_source_drift: SpecSourceDrift | None = None


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
    #: Every ``*.jsonl`` in the source's project directory as ``(name, mtime)``,
    #: mtime in epoch seconds (``None`` for one that could not be read). The
    #: TUPLE being ``None`` means nobody looked; an empty tuple means somebody
    #: looked and the directory holds no transcript, which is a real answer and a
    #: different one.
    transcripts: tuple[tuple[str, int | None], ...] | None = None
    #: The source's own ``runtime/<agent>/session_id`` — what its runtime last
    #: resumed. ``""`` means LOOKED AND FOUND NOTHING; ``None`` means nobody
    #: looked. The distinction decides a real case: with several transcripts and
    #: no marker the newest travels, but an UNREAD marker may name a different
    #: one, so it must not be treated as absent.
    session_marker: str | None = None


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
