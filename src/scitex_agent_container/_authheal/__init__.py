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
"""

from __future__ import annotations

from ._detect import DEFAULT_INTERVAL, capture_live_panes, detect_login_expired
from ._pass import (
    DEFAULT_PASS_CAP,
    AgentReport,
    PassOutcome,
    auth_heal_pass,
    history_path,
)

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_PASS_CAP",
    "AgentReport",
    "PassOutcome",
    "auth_heal_pass",
    "capture_live_panes",
    "detect_login_expired",
    "history_path",
]
