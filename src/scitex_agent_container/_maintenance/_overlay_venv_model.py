"""The OVERLAY VENV INVALIDATION CONTRACT — vocabulary and three-valued types.

THE CONTRACT
------------
**An image rebuild MUST invalidate the ``venv-sac`` slice of every per-agent
overlay.**

``/opt/venv-sac`` is IMAGE CONTENT that an overlay merely HAPPENS to be able to
write to. sac binds exactly one path into the venv —
``.../site-packages/scitex_agent_container`` — and everything else under
``/opt/venv-sac`` is baked into the SIF. The overlay's writability there is an
accident of how overlayfs unions a read-only lower with a writable upper, not a
statement that per-agent state belongs there. Per-agent state worth preserving
lives in the agent's HOME and its WORKDIR. Nothing under ``site-packages`` is
ever the agent's to keep.

Today the image is authoritative IN PRINCIPLE and silently overridden IN
PRACTICE by whichever files happen to be older. When an agent runs ``pip
install`` inside its container the result lands in the overlay upper and shadows
the image's copy FOREVER — including across an image rebuild, because nothing
re-evaluates the upper when the lower is swapped.

WHAT THAT COSTS, MEASURED
-------------------------
2026-08-11, host-side, under
``containers/overlays/<agent>/upper/opt/venv-sac/lib/python3.12/site-packages/``::

    scitex-dev 7 dist-infos (3 written that morning), scitex-agent-container 3,
    neurovista 2, paper-scitex-clew 2, scitex-db 2, scitex-hpc 2, scitex-hub 2,
    scitex-storage 2   — every other overlay 0

The SIF ships exactly ONE (``scitex_dev-0.43.1.dist-info``). The UNION is
incoherent: the image's dist-info advertises a ``pytest11`` entry point whose
module the stale overlay tree masks, so every ``pytest`` run dies before
collecting a single test::

    ModuleNotFoundError: No module named 'scitex_dev._core._test_execution_plugin'

It reads as a broken REPO. It is a broken ENV. scitex-hub lost a session to it.
A control confirmed the direction: a pruned overlay resolves ``0.43.1`` and
imports ``scitex_dev.store`` fine; an unpruned one resolves ``0.38.0`` and
raises ``ModuleNotFoundError``.

WHY THE KEY IS NOT THE FILENAME
-------------------------------
``containers/sac-base.sif`` is a STABLE SYMLINK::

    sac-base.sif -> .../containers/sac-base/sac-base-2026-0810-195145.sif

Its own name never changes, so keying invalidation on it could never detect a
rebuild — the check would pass forever, which is worse than no check because
the config still lists it. The identity is the RESOLVED TARGET (see
:func:`._overlay_venv_invalidate.sif_identity`).

THREE-VALUED, NEVER TWO
-----------------------
Every check here is PASS / FAIL / **UNKNOWN**, following the shape of
:mod:`.._lifecycle._relocate_preflight` (a sibling rail, deliberately not
imported so the two evolve independently). UNKNOWN refuses as firmly as FAIL
and prescribes "go measure it" rather than "go fix it". A check that cannot
read the overlay returns UNKNOWN, NEVER pass — folding "could not read" into
"fine" is the exact bug class this whole rail exists to close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ACTION_INVALIDATE",
    "ACTION_NONE",
    "ACTION_REFUSE",
    "CHECK_AGENT_NOT_RUNNING",
    "CHECK_BASE_USABLE",
    "CHECK_NOT_INSIDE_CONTAINER",
    "CHECK_OVERLAY_READABLE",
    "CHECK_SIF_IDENTITY",
    "CHECK_UPPER_NOT_MOUNTED_HERE",
    "CHECK_VENV_DISTS_UNIQUE",
    "InvalidationPlan",
    "OverlayVenvFacts",
    "VenvCheck",
]

# ---------------------------------------------------------------------------
# Check names (API strings — quoted in logs and in the health payload).
# ---------------------------------------------------------------------------
CHECK_NOT_INSIDE_CONTAINER: Final = "not_inside_container"
CHECK_AGENT_NOT_RUNNING: Final = "agent_not_running"
CHECK_UPPER_NOT_MOUNTED_HERE: Final = "upper_not_mounted_here"
CHECK_SIF_IDENTITY: Final = "sif_identity_resolved"
CHECK_OVERLAY_READABLE: Final = "overlay_readable"
CHECK_BASE_USABLE: Final = "base_layer_usable"

#: The BOOT ASSERTION's check name (:mod:`._venv_dist_assertion`).
CHECK_VENV_DISTS_UNIQUE: Final = "venv_distributions_unique"

# ---------------------------------------------------------------------------
# Actions. Deliberately not a bool: "nothing to do" and "I refuse to decide"
# are different answers with different follow-ups, and collapsing them is how
# a refusal gets read as an all-clear.
# ---------------------------------------------------------------------------
ACTION_NONE: Final = "none"
ACTION_INVALIDATE: Final = "invalidate"
ACTION_REFUSE: Final = "refuse"


@dataclass(frozen=True)
class VenvCheck:
    """One answer about one overlay.

    ``ok`` is three-valued: ``True`` pass, ``False`` fail, ``None`` COULD NOT
    DETERMINE. ``hint`` is required whenever the answer is not a pass — an
    error that only says what broke is half-written; it must also say what to
    do. Mirrors :class:`.._lifecycle._relocate_preflight.Check`.
    """

    name: str
    ok: bool | None
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("VenvCheck.name must be non-empty")
        if self.ok not in (True, False, None):
            raise ValueError(f"VenvCheck.ok must be True/False/None, got {self.ok!r}")
        if not self.detail:
            raise ValueError(f"VenvCheck {self.name!r}: detail must be non-empty")
        if self.ok is not True and not self.hint:
            raise ValueError(
                f"VenvCheck {self.name!r}: a non-passing check must carry a hint "
                "saying what to do about it"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class OverlayVenvFacts:
    """What was OBSERVED about one agent's overlay. Gathering happens elsewhere.

    Every ``| None`` field defaults to ``None`` meaning NOT OBSERVED — distinct
    from an observed negative. A prober that could not answer must say so
    rather than report a falsy default, because a default that reads as
    "absent" turns a failed probe into a confident wrong answer, and here the
    wrong answer MOVES FILES.
    """

    #: SIF identity of the image the agent is ABOUT to boot on. ``None`` = not
    #: observed; ``""`` = observed and unresolvable (both refuse).
    sif_identity: str | None = None

    #: Identity recorded in the overlay by the last reconcile. ``None`` = the
    #: stamp could not be read; ``""`` = readable and never stamped (which is
    #: every overlay in the fleet on the day this lands, and MUST invalidate —
    #: an unstamped overlay is exactly the state the measurement found).
    recorded_identity: str | None = None

    #: Does ``<overlay>/upper/opt/venv-sac`` exist? ``None`` = could not tell.
    venv_slice_present: bool | None = None

    #: Is THIS process running inside a container? ``None`` = could not tell.
    inside_container: bool | None = None

    #: Is the agent (and therefore its overlay mount) live? ``None`` = not
    #: observed.
    agent_running: bool | None = None

    #: Does this process's mount table show an overlayfs using this upper?
    #: ``None`` = the mount table could not be read.
    upper_mounted_here: bool | None = None

    #: Does the SIF's own ``venv-sac`` carry installed distributions — i.e. is
    #: there a KNOWN-GOOD LOWER LAYER to fall back on once the upper's slice is
    #: moved aside? ``None`` = not observed.
    #:
    #: This is the precondition the whole mutation rests on, and it was missing
    #: from the first cut of this rail. Moving the upper's slice aside when the
    #: lower is empty or unreadable does not restore the image's copy — it
    #: leaves the agent with NO venv at all, converting a shadowed-but-working
    #: container into a dead one. Expensive to observe (it needs an
    #: ``apptainer exec`` into the image), so it is gathered LAZILY: only when a
    #: move is actually on the table. See :func:`._overlay_venv_predicate.
    #: _check_base_usable` for why an unconsulted value is still a pass.
    base_provides_venv: bool | None = None


@dataclass(frozen=True)
class InvalidationPlan:
    """The whole answer for one overlay, in one shape.

    ``action`` is derived, never asserted independently of the checks: any
    FAIL or any UNKNOWN yields :data:`ACTION_REFUSE`, so there is no path on
    which an unresolved question results in a mutation.
    """

    agent: str
    overlay_root: str
    sif_identity: str = ""
    recorded_identity: str = ""
    checks: tuple[VenvCheck, ...] = field(default_factory=tuple)
    stale: bool = False
    venv_slice_present: bool = False

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("InvalidationPlan.agent must be non-empty")

    @property
    def failed(self) -> tuple[VenvCheck, ...]:
        return tuple(c for c in self.checks if c.ok is False)

    @property
    def unknown(self) -> tuple[VenvCheck, ...]:
        return tuple(c for c in self.checks if c.ok is None)

    @property
    def safe(self) -> bool | None:
        """Three-valued: may this plan touch the filesystem at all?"""
        if self.failed:
            return False
        if self.unknown:
            return None
        return True

    @property
    def action(self) -> str:
        if self.safe is not True:
            return ACTION_REFUSE
        if self.stale and self.venv_slice_present:
            return ACTION_INVALIDATE
        return ACTION_NONE

    def blocking_reasons(self) -> tuple[str, ...]:
        """Every reason this invalidation must not proceed, each with its hint.

        Failures first, then unknowns. Empty only when the answer is a clean
        go — which for this rail means "safe to act", not "action needed".
        """
        return tuple(
            f"{c.name}: {c.detail} -> {c.hint}" for c in (self.failed + self.unknown)
        )

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "overlay_root": self.overlay_root,
            "sif_identity": self.sif_identity,
            "recorded_identity": self.recorded_identity,
            "action": self.action,
            "safe": self.safe,
            "stale": self.stale,
            "venv_slice_present": self.venv_slice_present,
            "checks": [c.to_dict() for c in self.checks],
        }
