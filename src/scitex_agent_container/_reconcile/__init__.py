"""The fleet reconciler: the enforcer of "should be running ⇒ is running".

``restart: {policy: on-failure}`` sits in ~93 specs and, until this
package, did NOTHING. :mod:`.._lifecycle._start` starts the loop that reads
it (:func:`.._lifecycle.health.health_monitor`) on a ``daemon=True`` thread
and then returns — but ``sac agents start`` is a short-lived CLI, so that
thread dies with the process that made the promise. ``sac listen`` shells
out to that same short-lived CLI, so no long-lived process ever holds a
health monitor either; there is nothing here to race, only a hole to fill.
Meanwhile ``sac listen``'s own reconciler
(:mod:`.._listen._liveness_tick`) is explicit that "sac only DETECTS and
EMITS" — it alarms about stuck CARDS and never touches a process.

So nothing owned the promise. On the night an OAuth rotation killed 33
agents, they stayed dead until the operator happened to notice.

Layout — the rule is separated from the doing on purpose:

* :mod:`._rule`   — the PURE decision table (no IO, no clock).
* :mod:`._budget` — rate limits; a restart loop is worse than a down agent.
* :mod:`._alarm`  — the board rails: down cards + the reconciler's own
  heartbeat (who watches the watcher).
* :mod:`._pass`   — the IO: enumerate specs, observe tmux, restart corpses.

Driven by ``sac agents reconcile`` (dry-run by default) and scheduled as
the ``sac.fleet-reconcile`` JobSpec.
"""

from __future__ import annotations

from ._alarm import (
    SUBSYSTEM,
)
from ._budget import (
    DEBOUNCE_S,
    DEFAULT_PASS_CAP,
    MAX_RESTARTS_PER_AGENT_PER_HOUR,
    Budget,
    HistoryRead,
    HistoryState,
    read_history,
)
from ._pass import AgentReport, PassOutcome, fleet_spec_paths, reconcile_pass
from ._rule import MANAGED_POLICIES, Decision, Verdict, decide

__all__ = [
    "AgentReport",
    "Budget",
    "DEBOUNCE_S",
    "DEFAULT_PASS_CAP",
    "Decision",
    "HistoryRead",
    "HistoryState",
    "MANAGED_POLICIES",
    "MAX_RESTARTS_PER_AGENT_PER_HOUR",
    "PassOutcome",
    "Verdict",
    "SUBSYSTEM",
    "decide",
    "fleet_spec_paths",
    "read_history",
    "reconcile_pass",
]
