"""Shared autostart doctrine: a message to a stopped agent starts it.

Used at BOTH injection seams (``cli_pkg._send`` not-running branch and
the TUI turn-bridge TUI-down branch) so the two sides enforce one
doctrine, never two. See :mod:`cli_pkg._send_autostart` for the send-side
wiring.

Operator order 2026-10-16: EVERY send must kick, Hermes or not — a message
that lands on a stopped agent and dies with ``not running`` is a message
the fleet pretends was sent. The ``sac agents send`` not-running branch
used to do exactly that: return an error and drop the payload, leaving the
restart to the 5-minute reconcile beat, which restarts the corpse WITHOUT
the message.

This module closes the loop at the injection seam, reusing the reconcile
doctrine verbatim rather than inventing a second one:

* Only specs whose ``restart.policy`` is managed
  (:data:`MANAGED_POLICIES`) are eligible — the default ``"never"``
  means hands off.
* A last exit in :data:`DELIBERATE_EXIT_REASONS` (``sac agents stop`` /
  ``delete``) is sacred — the operator's own decision is never undone by
  a message arriving later.
* The shared restart budget (debounce + hourly cap, same history file
  the reconcile pass writes) applies — a send storm cannot restart-loop
  an agent that keeps dying.
* Every decline is LOUD: the caller gets the reason in the error payload,
  never a silent drop and never a misleading "dispatched".

Pure where it matters: policy lookup, row read, budget, clock and the
restart itself are all injectable seams, so tests pin the doctrine with
literal inputs. The live wiring passes the production seams.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from ._rule import DELIBERATE_EXIT_REASONS, MANAGED_POLICIES

__all__ = ["maybe_autostart_for_send"]


def _default_policy(name: str) -> str:
    from ..config import load_config
    from ..config._resolve import resolve_config

    return load_config(resolve_config(name)).restart.policy


def _default_row(name: str) -> Mapping[str, Any] | None:
    from .._state.state_store_instances import last_known_instance

    return last_known_instance(name)


def _default_budget():
    from ._budget import Budget, history_path, load_history

    return Budget(history=load_history(history_path()))


def _default_restart(name: str) -> bool:
    from .._lifecycle.lifecycle import agent_restart

    return agent_restart(name) is not False


def _default_record(budget, name: str, now: float) -> None:
    from ._budget import history_path, load_history, save_history

    history = load_history(history_path())
    history.setdefault(name, []).append(now)
    save_history(history_path(), history, now=now)
    budget.history = history


def maybe_autostart_for_send(
    name: str,
    *,
    policy_fn: Callable[[str], str] | None = None,
    row_fn: Callable[[str], Mapping[str, Any] | None] | None = None,
    budget=None,
    restart_fn: Callable[[str], bool] | None = None,
    record_fn: Callable[..., None] | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """Start ``name`` if doctrine allows; return (started, note).

    ``started=True`` means the agent was started by this call and the
    caller should re-resolve the endpoint and deliver. ``started=False``
    means hands off, and ``note`` names the exact reason — the caller
    puts it in the error payload so the refusal is always visible.
    """
    moment = time.time() if now is None else now
    try:
        policy = (policy_fn or _default_policy)(name)
    except Exception as exc:
        return False, f"could not read restart.policy: {exc}; not auto-starting"
    if policy not in MANAGED_POLICIES:
        return (
            False,
            f"restart.policy={policy!r} is not managed {sorted(MANAGED_POLICIES)}; "
            "not auto-starting",
        )
    try:
        row = (row_fn or _default_row)(name)
    except Exception as exc:
        return False, f"could not read last instance row: {exc}; not auto-starting"
    exit_reason = (row or {}).get("exit_reason") if row else None
    if exit_reason in DELIBERATE_EXIT_REASONS:
        return (
            False,
            f"last exit was deliberate ({exit_reason}); the operator's stop "
            "decision is sacred — not auto-starting",
        )
    own_budget = budget if budget is not None else _default_budget()
    try:
        check = own_budget.check(name, moment)
    except Exception as exc:
        return False, f"could not check restart budget: {exc}; not auto-starting"
    if not check.allowed:
        return False, f"restart budget declined ({check.reason}): {check.detail}"
    try:
        ok = (restart_fn or _default_restart)(name)
    except Exception as exc:
        return False, f"auto-start raised: {exc}"
    if not ok:
        return False, "auto-start attempt reported failure"
    try:
        (record_fn or _default_record)(own_budget, name, moment)
    except Exception as exc:
        return True, f"started, but budget record failed: {exc}"
    return True, "auto-started for send"
