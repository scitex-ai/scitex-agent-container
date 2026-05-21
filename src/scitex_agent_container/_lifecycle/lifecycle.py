"""Agent lifecycle management -- start, stop, restart, status.

This module is a thin orchestrator: the implementation lives in focused
sibling modules (split out for the 512-line module limit) and is
re-exported here so the long-standing public surface is unchanged for
both callers (``from .._lifecycle.lifecycle import agent_start``) and
tests (``lc._get_runtime`` / ``lc._run_hooks`` / ``lc.run_hook`` ...).

Implementation map:
    * :mod:`._start`          — :func:`agent_start`
    * :mod:`._stop`           — :func:`agent_stop`, :func:`agent_stop_all`,
                                :func:`agent_restart`
    * :mod:`._status`         — :func:`agent_status`, :func:`agent_logs`
    * :mod:`._runtime_select` — :func:`_get_runtime`, :func:`_fallback_workdir`
    * :mod:`._hook_runner`    — :func:`_run_hooks`, :func:`_fire_forget_hook`
    * :mod:`._session_reset`  — :func:`_clear_persisted_session_id`
    * :mod:`._handover_loader`— :func:`_load_handover_module`

Public injection seams (real-callable defaults):
    * ``runtime_factory`` — callable taking ``AgentConfig`` and returning a
      runtime object. Default: :func:`_get_runtime`. Used by every
      lifecycle entry point so callers (and tests) can substitute a real
      hand-rolled runtime collaborator without monkeypatching internals.
    * ``sleep_fn`` — callable matching ``time.sleep``. Default ``time.sleep``.
    * ``thread_factory`` — callable matching ``threading.Thread``. Default
      ``threading.Thread``. Used by :func:`agent_start` to spin up the
      health monitor thread.
    * ``handover_mod`` — module-like object exposing ``ensure_instance_uuid``,
      ``hydrate_from_hub``, ``push_pre_stop_snapshot``,
      ``start_failback_poller``. Default ``None`` — the real
      ``_lifecycle.handover`` module is imported lazily.
    * ``runner`` (``_run_hooks``) — callable matching ``subprocess.run``.

All defaults are real production callables, so the public API is unchanged
for existing callers; the seams are documented and tested rather than
hidden behind monkeypatch.
"""

from __future__ import annotations

# Re-export the hooks symbol tests patch via ``lc.run_hook``.
from ..hooks import run_hook
from ._handover_loader import _load_handover_module
from ._hook_runner import _fire_forget_hook, _run_hooks
from ._runtime_select import _fallback_workdir, _get_runtime
from ._session_reset import _clear_persisted_session_id
from ._start import agent_start
from ._status import agent_logs, agent_status
from ._stop import agent_restart, agent_stop, agent_stop_all

__all__ = [
    "agent_start",
    "agent_stop",
    "agent_stop_all",
    "agent_restart",
    "agent_status",
    "agent_logs",
    "run_hook",
    "_get_runtime",
    "_fallback_workdir",
    "_run_hooks",
    "_fire_forget_hook",
    "_clear_persisted_session_id",
    "_load_handover_module",
]
