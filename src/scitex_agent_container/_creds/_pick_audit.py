"""Auditable ranking-input record for the account pick.

Operator finding 2026-07-17: the ``[sac:creds] ... selected
credentials_files pool entry`` notice named its CRITERIA (token-fresh,
5h-blocked avoided, 7d-near-capped avoided, load-balanced) but not its
INPUTS — so when the pick happened to match what reset-time-awareness
would have chosen, nobody could tell a REASONED pick from a LUCKY one
(and it was lucky: both 7d resets were days out, far beyond the 2h
expiring horizon, so time-to-reset had zero effect and the
headroom-weighted rendezvous hash decided).

This module renders every candidate's full ranking inputs — 5h %, 7d %,
time-to-7d-reset, and the derived tier flags — into one log-safe line so
every pick is auditable after the fact.

Token safety: the audit reads ONLY utilisation percentages and reset
stamps (the same quota-cache / usage-cache values ``sac accounts list``
shows). No credential file is opened, no token value can appear in the
output — this is the pool behind the 2026-07-09 load/401 incident, and
a probe here must not mutate (or even read) a token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from ._quota_rank import (
    BLOCKED_5H_PCT,
    EXPIRING_7D_HORIZON_S,
    NEAR_CAP_7D_PCT,
    is_expiring_7d,
)
from ._spend_policy import coerce_epoch


@dataclass(frozen=True)
class CandidateAudit:
    """One candidate's full ranking inputs at pick time.

    Attributes
    ----------
    name
        The stored-account slug.
    pct_5h, pct_7d
        Cached window utilisation % (``None`` = unknown / no cache).
    reset_7d_in_s
        Seconds until the 7d window resets (negative = already past),
        or ``None`` when no reset stamp is cached.
    blocked_5h
        ``pct_5h`` at/above the blocked-now threshold (cannot serve a
        request immediately).
    near_capped_7d
        ``pct_7d`` at/above the near-cap threshold AND not expiring —
        the spread policy's avoid flag (burn inverts it into a reason
        to pick).
    expiring_7d
        The remaining 7d quota is use-it-or-lose-it right now
        (:func:`._quota_rank.is_expiring_7d`).
    """

    name: str
    pct_5h: float | None
    pct_7d: float | None
    reset_7d_in_s: float | None
    blocked_5h: bool
    near_capped_7d: bool
    expiring_7d: bool


def audit_candidates(
    names: list[str],
    usage_5h: Mapping[str, float | None],
    usage_7d: Mapping[str, float | None],
    *,
    reset_7d: Mapping[str, object] | None = None,
    now: float | None = None,
    near_cap_pct: float = NEAR_CAP_7D_PCT,
    blocked_5h_pct: float = BLOCKED_5H_PCT,
    expiring_horizon_s: float = EXPIRING_7D_HORIZON_S,
) -> list[CandidateAudit]:
    """Build one :class:`CandidateAudit` per candidate, in input order.

    Pure over its inputs (injectable ``now``); mirrors EXACTLY the
    derivations :func:`._quota_rank.pick_ranked` applies, so the audit
    line shows the same flags the ranking acted on.
    """
    _now = now if now is not None else time.time()
    _resets: Mapping[str, object] = reset_7d if reset_7d is not None else {}
    records: list[CandidateAudit] = []
    for name in names:
        h5 = usage_5h.get(name)
        d7 = usage_7d.get(name)
        reset_at = coerce_epoch(_resets.get(name))
        expiring = is_expiring_7d(d7, reset_at, _now, horizon_s=expiring_horizon_s)
        records.append(
            CandidateAudit(
                name=name,
                pct_5h=h5,
                pct_7d=d7,
                reset_7d_in_s=(reset_at - _now) if reset_at is not None else None,
                blocked_5h=h5 is not None and h5 >= blocked_5h_pct,
                near_capped_7d=(d7 is not None and d7 >= near_cap_pct and not expiring),
                expiring_7d=expiring,
            )
        )
    return records


def _fmt_pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _fmt_reset(seconds: float | None) -> str:
    """Hours-to-reset with sign: ``+56.1h`` / ``-0.2h`` / ``?``."""
    return "?" if seconds is None else f"{seconds / 3600.0:+.1f}h"


def format_pick_audit(records: list[CandidateAudit]) -> str:
    """One log-safe line naming EVERY ranking input for every candidate.

    ``ranking inputs: a(5h=9% 7d=10% 7d_reset=+56.4h), b(...)`` — the
    flags (``5h-blocked`` / ``7d-near-cap`` / ``7d-expiring``) appear
    only when set. Contains percentages, relative hours, and account
    slugs ONLY — never a token value or credential path.
    """
    parts: list[str] = []
    for r in records:
        flags = "".join(
            (
                " 5h-blocked" if r.blocked_5h else "",
                " 7d-near-cap" if r.near_capped_7d else "",
                " 7d-expiring" if r.expiring_7d else "",
            )
        )
        parts.append(
            f"{r.name}(5h={_fmt_pct(r.pct_5h)} 7d={_fmt_pct(r.pct_7d)} "
            f"7d_reset={_fmt_reset(r.reset_7d_in_s)}{flags})"
        )
    return "ranking inputs: " + ", ".join(parts)


__all__ = [
    "CandidateAudit",
    "audit_candidates",
    "format_pick_audit",
]
