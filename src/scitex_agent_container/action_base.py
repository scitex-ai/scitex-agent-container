"""Base class + engine for pane-mediated agent actions.

A :class:`PaneAction` subclass describes *what* to do in four small,
mostly-pure methods. The :func:`run_action` engine handles the shared
plumbing: precondition gate, send, completion polling, timeout,
logging to :mod:`action_store`, and uniform error handling.

Why a base class
----------------
The conversation that drove this design:

1. Every agent action we actually perform has the same shape:
   **detect state → send keystrokes with delays → ensure completion
   state → log the outcome**. Hand-rolling that loop per action
   produces drift (different timeout semantics, inconsistent
   logging, copy-pasted sleep/try/except trees).
2. Observer (``liveness_probe``) and actor code must stay split
   (operators may disable auto-response). This module is the actor
   half; it composes with, but does not import from, the observer.
3. Logs are for **ad-hoc** debugging and manual revising of the
   package — the engine writes one structured row per run and that
   is the only learning surface. There is no runtime callback that
   mutates module state based on outcomes.

Subclass contract
-----------------
A subclass implements four methods::

    class MyAction(PaneAction):
        name = "my-action"

        def snapshot(self, ctx) -> dict:
            # Whatever the action needs to judge completion.
            return {
                "pane_tail": ctx.capture_fn()[-2000:],
                "context_pct": ctx.context_pct_fn(),
            }

        def precheck(self, before: dict) -> bool:
            # Safe to act given the current state?
            return "auth_error" not in before["pane_tail"]

        def send(self, ctx) -> None:
            # Side-effect: issue keystrokes via ctx.mux.
            ctx.mux.send_text_and_submit(ctx.session, "/compact")

        def is_complete(self, before: dict, now: dict) -> bool:
            # Compare before vs now snapshots.
            return (before["context_pct"] or 100) - (now["context_pct"] or 100) >= 20

Subclasses never call ``time.sleep``, ``subprocess.run``, or write to
disk. Everything side-effecting flows through ``ctx`` or the engine.

Outcomes
--------
Every run ends in exactly one of:

* ``SUCCESS``               — ``is_complete`` became true before deadline.
* ``PRECONDITION_FAIL``     — ``precheck`` rejected before we acted.
* ``SEND_ERROR``            — ``send`` raised.
* ``COMPLETION_TIMEOUT``    — deadline reached without ``is_complete``.
* ``SKIPPED_BY_POLICY``     — caller passed ``skip_reason`` to ``run_action``.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Optional

from . import action_store

logger = logging.getLogger(__name__)


class ActionOutcome(str, Enum):
    """Canonical terminal states of one ``run_action`` call."""

    SUCCESS = "success"
    PRECONDITION_FAIL = "precondition_fail"
    SEND_ERROR = "send_error"
    COMPLETION_TIMEOUT = "completion_timeout"
    SKIPPED_BY_POLICY = "skipped_by_policy"


@dataclass
class ActionContext:
    """Everything a subclass needs to snapshot + send.

    All side-effecting callables live here so tests can inject fakes
    without monkey-patching subprocess or time.
    """

    agent: str
    session: str
    mux: Any  # TmuxManager | ScreenManager (see runtimes/multiplexer.py)
    capture_fn: Callable[[], str]
    context_pct_fn: Callable[[], Optional[float]] = field(
        default_factory=lambda: (lambda: None)
    )
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionAttempt:
    """Immutable record of one engine run. Mirrors a row in the
    ``attempts`` table."""

    agent: str
    action: str
    outcome: ActionOutcome
    elapsed_s: float
    started_at: str
    pane_before: Optional[dict[str, Any]]
    pane_after: Optional[dict[str, Any]]
    extras: dict[str, Any]

    def as_store_record(self) -> dict[str, Any]:
        """Shape expected by ``action_store.append_attempt``."""
        return {
            "ts": self.started_at,
            "agent": self.agent,
            "action": self.action,
            "outcome": self.outcome.value,
            "elapsed_s": self.elapsed_s,
            "pane_before": self.pane_before,
            "pane_after": self.pane_after,
            "extras": self.extras,
        }


class PaneAction(ABC):
    """Base class for one pane-mediated action."""

    name: ClassVar[str] = "unknown"

    # ---- subclass surface (4 abstract methods) ----------------------

    @abstractmethod
    def snapshot(self, ctx: ActionContext) -> dict[str, Any]:
        """Return the observation needed to judge completion.

        Called at least twice per run: once before ``send`` and once
        per poll afterwards. The contents are entirely up to the
        subclass — ``pane_tail``, ``context_pct``, ``pane_state``,
        any custom field — because the engine does not inspect them;
        it only forwards them to ``is_complete``/``precheck`` and
        stores them in the attempt log.
        """

    @abstractmethod
    def precheck(self, before: dict[str, Any]) -> bool:
        """Return True if the current state permits sending.

        Return False to abort with ``PRECONDITION_FAIL`` — typical
        reasons: the agent is already busy, the pane shows an auth
        error, a y/n prompt is pending the user's attention, etc.
        """

    @abstractmethod
    def send(self, ctx: ActionContext) -> None:
        """Side-effecting keystroke emission.

        Must only use ``ctx.mux`` + ``ctx.session``. Raising any
        exception terminates the run with ``SEND_ERROR``.
        """

    @abstractmethod
    def is_complete(self, before: dict[str, Any], now: dict[str, Any]) -> bool:
        """Pure comparison — True iff the action is done."""

    # ---- optional hooks (subclass may override) --------------------

    def before_send(self, ctx: ActionContext) -> None:
        """Hook for any prep work between precheck and send
        (e.g. mint a nonce). Default no-op."""
        del ctx

    def extras_at_end(self, ctx: ActionContext) -> dict[str, Any]:
        """Hook to emit action-specific fields for the attempt log
        (e.g. nonce value, command text). Default: ``ctx.extras``."""
        return dict(ctx.extras)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_action(
    action: PaneAction,
    ctx: ActionContext,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 2.0,
    skip_reason: Optional[str] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    store_root: Optional[Path] = None,
    write_to_store: bool = True,
) -> ActionAttempt:
    """Execute ``action`` against ``ctx`` and return the attempt record.

    Lifecycle::

        before = action.snapshot(ctx)            -- pre-state
        if skip_reason:                          -- policy skip
            -> SKIPPED_BY_POLICY, no send
        elif not action.precheck(before):
            -> PRECONDITION_FAIL, no send
        else:
            action.before_send(ctx)
            try: action.send(ctx)
            except: -> SEND_ERROR, no polling
            poll snapshot(ctx) every poll_interval until
                is_complete(before, now) -> SUCCESS
                or deadline -> COMPLETION_TIMEOUT

    Always writes one attempt row to :mod:`action_store` unless
    ``write_to_store=False`` (used by tests that want to inspect
    the returned ``ActionAttempt`` in isolation).
    """
    started_at = _now_iso()
    t0 = time_fn()

    def _finish(
        outcome: ActionOutcome,
        before: Optional[dict[str, Any]],
        after: Optional[dict[str, Any]],
    ) -> ActionAttempt:
        elapsed = time_fn() - t0
        attempt = ActionAttempt(
            agent=ctx.agent,
            action=action.name,
            outcome=outcome,
            elapsed_s=float(elapsed),
            started_at=started_at,
            pane_before=before,
            pane_after=after,
            extras=action.extras_at_end(ctx),
        )
        if write_to_store:
            action_store.append_attempt(attempt.as_store_record(), root=store_root)
        logger.info(
            "PaneAction %s on %s -> %s (%.2fs)",
            action.name,
            ctx.agent,
            outcome.value,
            attempt.elapsed_s,
        )
        return attempt

    # Pre-snapshot (if this fails the action is unrunnable).
    try:
        before = action.snapshot(ctx)
    except Exception as exc:  # pragma: no cover - defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning(
            "PaneAction %s: pre-snapshot raised %s; treating as SEND_ERROR",
            action.name,
            exc,
        )
        return _finish(ActionOutcome.SEND_ERROR, None, None)

    # Policy skip (caller decided not to run; we still log so the
    # skip is visible in the history).
    if skip_reason is not None:
        ctx.extras.setdefault("skip_reason", skip_reason)
        return _finish(ActionOutcome.SKIPPED_BY_POLICY, before, None)

    # Precondition gate.
    if not action.precheck(before):
        return _finish(ActionOutcome.PRECONDITION_FAIL, before, None)

    # Send.
    try:
        action.before_send(ctx)
        action.send(ctx)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("PaneAction %s: send raised %s", action.name, exc)
        ctx.extras.setdefault("send_error", f"{type(exc).__name__}: {exc}")
        return _finish(ActionOutcome.SEND_ERROR, before, None)

    # Poll until complete or deadline.
    deadline = t0 + float(timeout_s)
    now_snap = before  # default if we never poll (timeout_s <= 0)
    while True:
        current = time_fn()
        try:
            now_snap = action.snapshot(ctx)
        except Exception as exc:  # pragma: no cover - defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            logger.debug(
                "PaneAction %s: snapshot during polling raised %s",
                action.name,
                exc,
            )
            # Keep the previous snapshot so is_complete sees something.
        try:
            done = action.is_complete(before, now_snap)
        except Exception as exc:  # pragma: no cover - defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            logger.debug("PaneAction %s: is_complete raised %s", action.name, exc)
            done = False
        if done:
            return _finish(ActionOutcome.SUCCESS, before, now_snap)
        if current >= deadline:
            return _finish(ActionOutcome.COMPLETION_TIMEOUT, before, now_snap)
        sleep_fn(poll_interval_s)
