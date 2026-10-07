"""Keep historical TUI banners out of destructive restart admission.

The positional and liveness auditors deliberately cannot authorize a restart
until a production true positive establishes their boundary.  This adapter
applies that contract to the scheduled pass, rather than only its audit CLI.
Native Hermes needs session/turn evidence; a Claude-style banner is not that
evidence, even when a separately verified successor could start successfully.
"""

from __future__ import annotations

from dataclasses import dataclass

from .._reconcile._rule import Verdict
from ._liveness import corroborate
from ._positional import classify_positional
from ._reports import AgentReport


@dataclass(frozen=True)
class BannerAdmission:
    allowed: bool
    report: AgentReport


def banner_restart_admission(
    name: str,
    panes: tuple[str | None, str | None],
    *,
    observed_s: float,
) -> BannerAdmission:
    """Report what two panes prove without inferring an assigned-work stall."""
    before, after = panes
    positional = classify_positional(name, after)
    liveness = corroborate(name, before, after, observed_s=observed_s)
    allowed = positional.may_restart and liveness.may_restart
    reason = (
        "historical-auth-banner"
        if positional.banner_lines
        and not positional.banners_below
        and positional.marker_line is not None
        else "auth-banner-unproven"
    )
    return BannerAdmission(
        allowed=allowed,
        report=AgentReport(
            name,
            Verdict.UNOBSERVED,
            reason,
            f"{name}: auth banner is report-only; NOT restarted. "
            f"positional={positional.state}, liveness={liveness.state}. "
            "A frozen or historical banner cannot distinguish healthy idle "
            "from a current auth failure. Native Hermes requires session/turn "
            "telemetry proving assigned work is stalled; successor credential "
            "health does not prove the current session is wedged.",
        ),
    )
