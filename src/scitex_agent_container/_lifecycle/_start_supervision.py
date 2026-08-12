"""The background threads ``agent_start`` leaves running behind a started agent.

Extracted from :mod:`._start` under the same 512-line cap that already split
``_start_announce`` / ``_start_failure_diag`` / ``_start_preflight`` out of it.

Both threads here share one shape and one rule: they are DAEMON threads that
supervise an agent which is ALREADY up, and neither may take the start down
with it. A supervisor that can fail a start it was added to protect is an
outage generator — so a failure in either is printed and stepped over, and
``agent_start`` still returns True for the agent that did, in fact, start.
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from .._state.registry import Registry
from ..config import AgentConfig
from ._instances import make_restart_callback as _make_restart_callback
from .health import health_monitor


def start_background_supervision(
    config: AgentConfig,
    *,
    registry: Registry,
    runtime_factory: Callable[..., Any],
    handover: Any,
    thread_factory: Callable[..., Any] = threading.Thread,
) -> None:
    """Launch the post-start daemon threads for an agent that is now up.

    Two independent supervisors:

    * **Health monitor** — only when ``spec.health.enabled``. Its restart
      callback re-records the ``instances`` row (a restart is a NEW pid) AND
      pins the state.db it writes to; see
      :func:`._instances.make_restart_callback`.
    * **Priority-failback poller** (ZOO#12 FR-B) — a no-op when the spec has no
      ``priority_list``; otherwise polls the hub every 60 s and steps aside
      (snapshot push + SIGTERM) once a higher-priority host is healthy.

    ``handover`` is the resolved handover module (``agent_start``'s ``_h``),
    passed in rather than re-loaded so both call sites share one instance.
    """
    if config.health.enabled:
        thread = thread_factory(
            target=health_monitor,
            args=(
                config.name,
                config,
                registry,
                _make_restart_callback(runtime_factory),
            ),
            daemon=True,
        )
        thread.start()

    # stx-allow: fallback (reason: the failback poller is an optional optimisation for multi-host specs; a failure to launch it must not fail a start that has already succeeded — it is printed in full, never swallowed)
    try:
        handover.start_failback_poller(config)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        traceback.print_exc()


__all__ = ["start_background_supervision"]
