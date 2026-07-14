"""The freshness verdict model — three states, and UNKNOWN is not "fine".

This is the deploy-side sibling of :mod:`.._drift._status`. ``_drift``
compares a git checkout against its upstream (behind / ahead / diverged);
``_freshness`` compares what is *deployed* against what actually
*shipped* (installed vs PyPI, tag vs PyPI, running vs installed).

Different domain, same hard-won doctrine, stated once here:

    A check that cannot reach its evidence reports UNKNOWN. It never
    reports FRESH.

Both halves of that sentence are load-bearing, and both were paid for:

* Treating "no evidence" as healthy is how a fix sits un-shipped for a
  day while agents re-diagnose it. Tags v0.21.15 and v0.21.16 never
  reached PyPI, and nothing anywhere said a word.
* Treating "no evidence" as *broken* is worse, because a false RED gets
  a remedy, and the remedy destroys a healthy thing. So only
  :attr:`Freshness.STALE` — positive evidence of staleness — is ever
  actionable. UNKNOWN is silent by construction.

``_drift`` reached the same conclusion in its own vocabulary; see
``DriftStatus.is_drifted``: "NOT_A_REPO / UNREACHABLE are NOT drift --
drift is *unknown* there, not present."
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

__all__ = ["Finding", "Freshness", "FreshnessReport"]


class Freshness(enum.Enum):
    """The verdict for one deploy-freshness check.

    * ``FRESH``   — positive evidence the thing is current.
    * ``STALE``   — positive evidence the thing is behind. Actionable.
    * ``UNKNOWN`` — the evidence could not be obtained (offline, no such
      file, unparseable version, daemon not under systemd, ...). NOT a
      synonym for FRESH. Never actionable, never raised as an alarm.
    """

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """The outcome of a single named check.

    Attributes:
        check: Stable machine id (``host-behind-pypi``, ``ghost-tag``,
            ``running-vs-installed``, ``release-run``, ``symbol-probe``).
        state: The :class:`Freshness` verdict.
        summary: One human line. Shown to the operator when STALE.
        remedy: The exact command that fixes it. Empty when the fix is
            not a command a human can just run (e.g. a failed CI run).
            An alarm that does not say what to *do* gets ignored.
        detail: Longer context; shown in ``--json`` and verbose output.
        data: Machine-readable evidence for this finding.
    """

    check: str
    state: Freshness
    summary: str
    remedy: str = ""
    detail: str = ""
    data: dict = field(default_factory=dict)

    @property
    def is_stale(self) -> bool:
        """True only on positive evidence of staleness.

        UNKNOWN is deliberately excluded — see the module docstring.
        """
        return self.state is Freshness.STALE

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "state": self.state.value,
            "summary": self.summary,
            "remedy": self.remedy,
            "detail": self.detail,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Finding":
        """Rebuild from :meth:`to_dict` (the cache round-trip).

        An unrecognised state string decays to UNKNOWN rather than
        raising: a cache written by a future version must never break
        the CLI that reads it.
        """
        try:
            state = Freshness(raw.get("state"))
        except ValueError:
            state = Freshness.UNKNOWN
        return cls(
            check=str(raw.get("check", "")),
            state=state,
            summary=str(raw.get("summary", "")),
            remedy=str(raw.get("remedy", "")),
            detail=str(raw.get("detail", "")),
            data=dict(raw.get("data") or {}),
        )


@dataclass(frozen=True)
class FreshnessReport:
    """All findings from one refresh, plus the aggregate verdict."""

    findings: tuple[Finding, ...] = ()
    generated_at: float = 0.0

    @property
    def state(self) -> Freshness:
        """Aggregate verdict. Precedence is STALE > UNKNOWN > FRESH.

        * any STALE -> STALE. Positive evidence of a problem outranks
          everything; one un-shipped fix still matters when four other
          checks are clean.
        * else any UNKNOWN -> UNKNOWN. Partial blindness is never
          summarised as "fresh".
        * else FRESH.

        No findings at all is UNKNOWN, not FRESH — an empty report means
        nothing was checked, which is exactly the state this module
        exists to stop being read as good news.
        """
        if not self.findings:
            return Freshness.UNKNOWN
        if any(f.state is Freshness.STALE for f in self.findings):
            return Freshness.STALE
        if any(f.state is Freshness.UNKNOWN for f in self.findings):
            return Freshness.UNKNOWN
        return Freshness.FRESH

    @property
    def stale(self) -> tuple[Finding, ...]:
        """Just the actionable findings — all an alarm may speak about."""
        return tuple(f for f in self.findings if f.is_stale)

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "generated_at": self.generated_at,
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FreshnessReport":
        return cls(
            findings=tuple(Finding.from_dict(f) for f in (raw.get("findings") or [])),
            generated_at=float(raw.get("generated_at") or 0.0),
        )


# EOF
