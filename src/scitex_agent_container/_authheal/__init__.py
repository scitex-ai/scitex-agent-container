"""Auto-restart LIVE agents wedged behind a frozen "Login expired" banner.

The sibling of :mod:`.._reconcile`: fleet-reconcile restarts DEAD (no tmux
session) agents; this restarts LIVE-session-but-auth-dead ones — the case
fleet-reconcile explicitly leaves alone. Detection is READ-ONLY and
2-run-corroborated (:mod:`._detect`); the restart is rate-limited and escalates
to a board card instead of an infinite bounce (:mod:`._pass`, :mod:`._alarm`).

Surfaced as ``sac agents restart-login-expired`` and the federated
``sac.restart-login-expired-agents`` timer job. READ THE DEPLOY GATE in
:mod:`._pass` before enabling that timer — an existing ``auth-heal.py`` cron
already restarts these agents, and two restarters on one fleet is the
double-supervisor class.

THE BANNER DETECTORS HERE PRODUCE FALSE POSITIVES — READ THIS FIRST
    Proven on a real captured pane (checked in as
    ``tests/.../specimen_grant_20260718_alive_false_positive.log``): on
    2026-07-18 ``grant`` was reported AUTH-FAILED by ``auth-status`` and STUCK
    by ``auth-heal`` while it was ALIVE and working — answering a ping, reading
    files, running shell commands, finishing a background publish.

    The mechanism is not scrollback: both detectors capture the VISIBLE screen
    only (``capture-pane -p``, no ``-S``). It is that a TRAILING BANNER means
    "the last thing this agent RENDERED was a banner", which is not "this agent
    is broken NOW". An agent that hit a 401, recovered and went idle renders
    nothing further, so the banner stays on screen indefinitely.

    The "frozen across two runs" hardening INVERTS: an idle agent's pane does
    not change, so a stale banner on a healthy idle agent looks maximally
    frozen. Freeze corroborates idleness — the one property the wedged and the
    recovered-idle agent share — and reports the confusion confidently.

    :mod:`._positional` is the proposed replacement (the operator's rule): a
    banner ABOVE the last startup marker is HISTORY, one BELOW it is CURRENT.
    :mod:`._liveness` adds the only non-invasive positive evidence of life — did
    the pane change. Both are REPORT-ONLY, surfaced as ``sac agents auth-audit``.

    NO AUTOMATED RESTARTER SHIPS FROM THIS PACKAGE until that audit is clean
    across the fleet AND a true-positive pane has been captured. ``auth-heal``
    logged 167 auto-restarts in 7 days on this signal.
"""

from __future__ import annotations

from ._detect import (
    DEFAULT_INTERVAL,
    DetectionOutcome,
    Roster,
    capture_live_panes,
    capture_live_panes_once,
    detect_login_expired,
    registered_agents,
)
from ._journal import Journal, log_path
from ._liveness import DEFAULT_OBSERVE_S, Liveness, corroborate
from ._pass import (
    DEFAULT_PASS_CAP,
    AgentReport,
    PassOutcome,
    auth_heal_pass,
    history_path,
)
from ._positional import (
    ALIVE,
    DEAD,
    STARTUP_MARKER,
    UNKNOWN,
    PositionalVerdict,
    classify_positional,
)
from ._specimen import Specimen, capture_specimen, specimen_path

__all__ = [
    "ALIVE",
    "DEAD",
    "DEFAULT_INTERVAL",
    "DEFAULT_OBSERVE_S",
    "DEFAULT_PASS_CAP",
    "STARTUP_MARKER",
    "UNKNOWN",
    "AgentReport",
    "DetectionOutcome",
    "Journal",
    "Liveness",
    "PassOutcome",
    "PositionalVerdict",
    "Roster",
    "Specimen",
    "auth_heal_pass",
    "capture_live_panes",
    "capture_live_panes_once",
    "capture_specimen",
    "classify_positional",
    "corroborate",
    "detect_login_expired",
    "history_path",
    "log_path",
    "registered_agents",
    "specimen_path",
]
