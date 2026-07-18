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

TWO DISCRIMINATORS LIVE HERE, AND THE NEWER ONE IS STRICTLY WIDER
    ``restart-login-expired`` (:mod:`._pass`) flags an agent only when its
    banner is DISTANCE-FROZEN across two captures. That test calls an agent
    HEALTHY whenever its pane still moves, so an animating-but-wedged agent — a
    spinner, a clock, a countdown, a reflowing redraw — is missed, and those are
    the agents the operator then restarts by hand.

    ``restart-login-required`` (:mod:`._nearprompt`, :mod:`._restart_pass`) is
    the correction: it keeps the anti-prose defence but takes it from the
    NEAR-PROMPT geometry of a SINGLE capture — is the banner the current UI
    state, or is it scrollback text? — so animation cannot hide a wedge. It
    restarts through the operator's own verified ``sac agents restart -y
    <name>`` and writes every verdict, reason, pane, command, exit code, stdout
    and stderr to a log (:mod:`._journal`).
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
from ._nearprompt import (
    VERDICT_LOGIN_REQUIRED,
    VERDICT_OK,
    VERDICT_UNKNOWN,
    Finding,
    classify_pane,
    classify_panes,
)
from ._pass import (
    DEFAULT_PASS_CAP,
    AgentReport,
    PassOutcome,
    auth_heal_pass,
    history_path,
)
from ._restart_cmd import RestartResult, restart_command, run_sac_restart
from ._restart_pass import restart_login_required_pass

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_PASS_CAP",
    "VERDICT_LOGIN_REQUIRED",
    "VERDICT_OK",
    "VERDICT_UNKNOWN",
    "AgentReport",
    "DetectionOutcome",
    "Finding",
    "Journal",
    "PassOutcome",
    "RestartResult",
    "Roster",
    "auth_heal_pass",
    "capture_live_panes",
    "capture_live_panes_once",
    "classify_pane",
    "classify_panes",
    "detect_login_expired",
    "history_path",
    "log_path",
    "registered_agents",
    "restart_command",
    "restart_login_required_pass",
    "run_sac_restart",
]
