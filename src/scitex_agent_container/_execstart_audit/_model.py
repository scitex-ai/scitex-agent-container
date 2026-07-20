"""Verdict vocabulary and report types for the ExecStart audit.

Split out of the package ``__init__`` purely for size; the doctrine these
types encode is documented there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecVerdict(str, Enum):
    """Five states. UNKNOWN and UNVERIFIABLE are refusals to guess.

    * ``MATCH`` — the unit runs exactly what the JobSpec declares.
    * ``DIVERGED`` — it does not. An unmanaged override, or a generator bug.
    * ``NOT_INSTALLED`` — nothing is deployed for this declaration. Not a
      divergence: several sac jobs are declared behind a deliberate
      deploy gate.
    * ``UNVERIFIABLE`` — the declared command's head is not absolute, so
      the intended value is not reproducible in this interpreter. The
      check refuses to compare rather than emit a meaningless verdict.
    * ``UNKNOWN`` — systemd could not be asked at all.
    """

    MATCH = "match"
    DIVERGED = "diverged"
    NOT_INSTALLED = "not-installed"
    UNVERIFIABLE = "unverifiable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnitState:
    """What systemd said about one unit.

    ``load_state=None`` together with a non-empty ``error`` is the UNKNOWN
    carrier: systemd could not be asked, and ``error`` says why.
    """

    load_state: str | None
    execstart: str | None
    error: str | None = None


@dataclass(frozen=True)
class ExecFinding:
    """One job, what its unit runs, and what the source intended."""

    job: str
    unit: str
    verdict: ExecVerdict
    detail: str
    intended: str | None = None
    resolved: str | None = None

    def __post_init__(self) -> None:
        # Validate at construction so a malformed finding crashes HERE and
        # not inside a report someone acts on. Same doctrine as JobSpec.
        if not isinstance(self.verdict, ExecVerdict):
            raise ValueError(
                f"ExecFinding.verdict must be an ExecVerdict, got {self.verdict!r}"
            )
        if not self.job:
            raise ValueError("ExecFinding.job must be non-empty")
        if not self.detail:
            # A verdict with no stated evidence is a postmortem in a
            # comment, which is what this module exists to replace.
            raise ValueError(
                f"ExecFinding({self.job!r}).detail must state the evidence"
            )
        if self.verdict is ExecVerdict.DIVERGED and not (
            self.intended and self.resolved
        ):
            # A divergence that does not carry BOTH sides is unactionable —
            # the reader cannot tell which side to change.
            raise ValueError(
                f"ExecFinding({self.job!r}): a DIVERGED finding must carry "
                f"both intended and resolved"
            )


@dataclass(frozen=True)
class ExecStartReport:
    """The audit result. Fails LOUD via :meth:`render`, never silently."""

    findings: tuple[ExecFinding, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(f, ExecFinding) for f in self.findings):
            raise ValueError("ExecStartReport.findings must all be ExecFinding")

    def of(self, verdict: ExecVerdict) -> tuple[ExecFinding, ...]:
        return tuple(f for f in self.findings if f.verdict is verdict)

    @property
    def diverged(self) -> tuple[ExecFinding, ...]:
        return self.of(ExecVerdict.DIVERGED)

    @property
    def unknown(self) -> tuple[ExecFinding, ...]:
        return self.of(ExecVerdict.UNKNOWN)

    @property
    def unverifiable(self) -> tuple[ExecFinding, ...]:
        return self.of(ExecVerdict.UNVERIFIABLE)

    @property
    def ok(self) -> bool:
        """True only when nothing DIVERGED.

        UNKNOWN deliberately does NOT make this False: "I could not ask"
        is not "I found a problem", and conflating them would make the
        check permanently red anywhere systemd is absent (every
        container, all of CI) — which is how a check gets muted. The
        UNKNOWNs are still rendered, loudly, so the reader always knows
        how much was actually checked.
        """
        return not self.diverged

    def render(self) -> str:
        """A report that names each divergence and BOTH sides of it."""
        lines: list[str] = []
        if self.diverged:
            lines.append(
                f"{len(self.diverged)} UNIT(S) DO NOT RUN WHAT THE SOURCE "
                f"DECLARES — an unmanaged local override, or a generator bug:"
            )
            for f in self.diverged:
                lines.append(f"  {f.job}  ({f.unit})")
                lines.append(f"      intended: {f.intended}")
                lines.append(f"      resolved: {f.resolved}")
                lines.append(f"      {f.detail}")
        else:
            lines.append("no ExecStart divergence found")

        for f in self.unverifiable:
            lines.append(f"  [unverifiable] {f.job}: {f.detail}")
        if self.unknown:
            lines.append(
                f"  ({len(self.unknown)} UNKNOWN — could not ask systemd; "
                f"this is NOT a pass, and nothing here was checked)"
            )
            for f in self.unknown:
                lines.append(f"      {f.job}: {f.detail}")
        return "\n".join(lines)


__all__ = ["ExecFinding", "ExecStartReport", "ExecVerdict", "UnitState"]
