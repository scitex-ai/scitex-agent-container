"""SDK hook callback bridge for the claude-session runner.

Wires Anthropic's hook event taxonomy
(``PreToolUse`` / ``PostToolUse`` / ``UserPromptSubmit`` / ``Stop``)
to ``scitex_agent_container.event_log.append_event`` using the same
record schema the legacy CLI runtime publishes via
``sac record-hook-event``. Keeping that schema identical means
downstream consumers (``sac agent status``, ``event_log.summarize``,
fleet dashboards) work unchanged when an agent flips runtimes.

Hook callbacks are *async no-ops on the wire*: they return ``{}`` to
the SDK and never block its control flow. ``append_event`` is itself
swallowed-failure, so a misbehaving hook cannot kill the agent.

Stop-hook completion push (requester-feedback)
----------------------------------------------
The ``Stop`` hook additionally PUSHes a completion report back to the
turn's *requester* — the peer that dispatched the turn, threaded onto
the ``TurnEnvelope`` as ``from_agent`` + ``dispatch_id`` and surfaced to
the hook via a per-conversation :class:`TurnContext` holder. This is the
push-feedback north star: a requester hears about completion without
polling, and it generalises to ANY peer (the lead is not special-cased).

The push is wired through two injected seams so it stays testable
end-to-end without mocks:

* ``turn_context`` — a mutable holder the conversation task updates at
  the start/end of each turn (requester, dispatch_id, summary, status).
  Turns are serial, so one holder is race-free.
* ``push_fn`` — an async ``(report, requester, dispatch_id) -> None``
  callable that performs the actual delivery. The runner injects a
  closure over :func:`_session_completion.push_completion`; tests inject
  a closure that POSTs to a real local receiver.

A push failure is LOUD (logged at WARNING with the requester named) but
does NOT crash the agent — the turn already completed; the hook returns
``{}`` regardless. When the turn had no requester, the push is skipped
(a mission/boot turn answers to no peer — not an error).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Async seam: (report, requester, dispatch_id) -> awaitable. The runner
# binds it to the real completion-push emitter; tests bind it to a real
# local-receiver-backed closure. No mock surface.
PushFn = Callable[[dict, str, Optional[str]], Awaitable[None]]


@dataclass
class TurnContext:
    """Per-conversation holder for the in-flight turn's requester identity.

    The conversation task calls :meth:`begin` when it picks an envelope and
    :meth:`finish` when the turn drains (or fails). The Stop hook reads the
    holder to address its completion push. Turns are serial, so a single
    mutable holder is correct — there is never more than one in-flight turn.

    ``status`` is the honest outcome the conversation records: ``None``
    before a turn, then one of the ``_session_completion`` status strings.
    """

    requester: Optional[str] = None
    dispatch_id: Optional[str] = None
    summary: str = ""
    status: Optional[str] = None
    pushed: bool = field(default=False, repr=False)

    def begin(self, *, requester: Optional[str], dispatch_id: Optional[str]) -> None:
        """Open a new turn: record its requester + dispatch id, reset outcome.

        Resets ``pushed`` so each turn gets exactly one completion push —
        the guard the Stop hook and the failure path share so a turn that
        both errors AND fires Stop cannot double-report.
        """
        self.requester = requester
        self.dispatch_id = dispatch_id
        self.summary = ""
        self.status = None
        self.pushed = False

    def finish(self, *, status: str, summary: str) -> None:
        """Close the turn with its honest outcome + reply summary."""
        self.status = status
        self.summary = summary


async def emit_completion_push(
    turn_context: Optional[TurnContext],
    push_fn: Optional[PushFn],
    *,
    agent_name: str,
) -> None:
    """PUSH the current turn's completion report to its requester (once).

    The single shared emit path used by BOTH the Stop hook (clean-turn-end
    signal) and the conversation's failure branch (the SDK raised before a
    clean Stop). The ``turn_context.pushed`` guard ensures exactly one push
    per turn — a turn that errors AND somehow also fires Stop reports once.

    Skips quietly (returns) when:

    * the seams aren't wired (``turn_context`` / ``push_fn`` is ``None``) —
      a bare runner with no host control-plane has nowhere to push;
    * the turn had no requester (a mission/boot turn answers to no peer);
    * this turn was already pushed; or
    * the push would not carry information the requester can act on.
      The CATEGORICAL guard (lead 2026-06-07, #326-part-2): suppress
      UNLESS BOTH ``status in {SUCCESS, FAILURE}`` AND
      ``summary_stripped`` is non-empty. The previous union-style guard
      still leaked ~4% of empty beacons (60 wild leaks observed under
      #326 in a single 30-min idle window, all ``status="unknown"``,
      escaping the union because some path repopulated ``.summary``
      without flipping ``.status``). The categorical form removes the
      ambiguity: no real outcome OR no content → drop. Anything else
      is signal and flows.

    Status is taken verbatim from ``turn_context.status`` — set honestly by
    the conversation (``success`` on clean drain, ``failure`` on SDK error).
    A ``None`` status (the push fired before the conversation recorded an
    outcome — should not happen, since Stop comes after the ResultMessage)
    is reported as ``unknown``, NEVER fabricated as success.

    A diagnostic ``log.warning("completion-push emit-state ...")`` is
    emitted on every reachable call so the next SIF cycle captures the
    actual emit-state at leak-time — root-causing both the residual
    leak path AND bug #2 (legitimate success pushes silently suppressed
    by a Stop-hook-fires-before-finally race that flips ``pushed=True``
    prematurely). The diagnostic stays in until bug #2 is filed and
    root-caused; it can then drop to DEBUG.

    A delivery failure is LOUD (logged at WARNING, requester named) but
    never re-raised: the turn already completed, and a flaky receipt must
    not crash the agent or abort the SDK control flow.
    """
    if turn_context is None or push_fn is None:
        return
    requester = turn_context.requester
    if not requester or turn_context.pushed:
        return

    from ._session_completion import (
        STATUS_FAILURE,
        STATUS_SUCCESS,
        STATUS_UNKNOWN,
        build_completion_report,
    )

    status = turn_context.status or STATUS_UNKNOWN
    summary_stripped = (turn_context.summary or "").strip()

    # Diagnostic: stamp WHO called us + the full state at emit time so
    # we can correlate against the stdout warning + the inbox-side leak
    # next SIF cycle. Stack-introspection (rather than a new kwarg) keeps
    # the seam stable for the two call sites (Stop hook + _drive_turn
    # finally) and any future caller. Bounded summary repr so a runaway
    # turn cannot bloat the log line.
    _caller_frame = sys._getframe(1)
    _caller_name = (
        f"{_caller_frame.f_code.co_filename.rsplit('/', 1)[-1]}:"
        f"{_caller_frame.f_code.co_name}"
    )
    log.warning(
        "completion-push emit-state requester=%r dispatch_id=%r "
        "status=%r summary=%r pushed=%r caller=%s",
        requester,
        turn_context.dispatch_id,
        turn_context.status,
        (turn_context.summary or "")[:80],
        turn_context.pushed,
        _caller_name,
    )

    # Categorical fleet-noise guard (#326-part-2, 2026-06-07).
    #
    # A completion push only carries information for the requester when
    # BOTH legs are honest:
    #   * ``status in {SUCCESS, FAILURE}`` — the conversation reached a
    #     real outcome (clean ResultMessage or a caught exception). Any
    #     other status, including the ``STATUS_UNKNOWN`` coercion of a
    #     ``None`` ``turn_context.status``, means the emit fired before
    #     the conversation recorded an outcome — by definition nothing
    #     to report.
    #   * ``summary_stripped`` is non-empty — the assistant produced
    #     content the requester can read. A whitespace-only summary
    #     counts as empty (strip first).
    #
    # If EITHER leg fails, drop. The previous union-style guard
    # (``status == UNKNOWN`` OR ``dispatch_id is None``, gated on empty
    # summary) still leaked: in a single 30-min idle window on the
    # post-#326 SIF, 60 empty ``status="unknown"`` beacons escaped to
    # clew/neurovista alongside ZERO ``status="success"`` failures —
    # i.e. some path repopulated ``.summary`` without flipping
    # ``.status``, slipping past the ``not summary_stripped`` clause.
    # The categorical form removes that ambiguity by predicating on the
    # honest-state AND-condition the requester actually needs.
    #
    # Bug #2 follow-up (separate from this guard): the absence of any
    # ``status="success"`` failure in the same window strongly suggests
    # legitimate success pushes are being suppressed by a Stop-hook /
    # finally race that flips ``pushed=True`` before the success state
    # is recorded. Root-caused later with the diagnostic above; the
    # operator's empty-ping goal is met without it.
    if status not in (STATUS_SUCCESS, STATUS_FAILURE) or not summary_stripped:
        turn_context.pushed = True
        return

    turn_context.pushed = True

    report = build_completion_report(
        agent=agent_name,
        dispatch_id=turn_context.dispatch_id,
        status=status,
        summary_text=turn_context.summary,
    )
    try:
        await push_fn(report, requester, turn_context.dispatch_id)
    except Exception as exc:  # stx-allow: fallback (reason: a failed completion push must be LOUD but must not crash the agent — the turn already completed; logged at WARNING, never re-raised)
        log.warning(
            "completion push to requester %r failed (status=%s dispatch_id=%s): %s",
            requester,
            status,
            turn_context.dispatch_id,
            exc,
        )


def build_event_log_hooks(
    agent_name: str,
    hook_matcher_cls: Any,
    *,
    event_log_root: Path | None = None,
    turn_context: TurnContext | None = None,
    push_fn: PushFn | None = None,
) -> dict:
    """Return the ``hooks=`` dict passed to ``ClaudeAgentOptions``.

    Each event class registers exactly one matcher with one callback;
    the callback forwards the SDK payload's relevant fields to
    ``event_log.append_event`` under the matching legacy ``kind``.

    ``event_log_root`` (optional) is forwarded to ``append_event(..., root=)``
    so tests can redirect the ring-buffer to a tmp dir without monkey-
    patching the production helper.

    ``turn_context`` + ``push_fn`` (optional, both required together to
    enable the push) wire the Stop-hook completion push. When supplied, the
    Stop hook reads the just-finished turn's requester from ``turn_context``
    and, if a requester is present, calls ``push_fn(report, requester,
    dispatch_id)``. Absent either seam, the Stop hook keeps its prior
    behaviour (event-log append only) — so legacy callers and the mission
    boot path are unaffected.
    """
    from .._state.event_log import append_event

    async def _on_pretool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "pretool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
            },
            root=event_log_root,
        )
        return {}

    async def _on_posttool(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "posttool",
            {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
                "tool_response": payload.get("tool_response"),
            },
            root=event_log_root,
        )
        return {}

    async def _on_prompt(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "prompt",
            {"prompt": payload.get("prompt", "")},
            root=event_log_root,
        )
        return {}

    async def _on_stop(payload, _tool_use_id, _ctx):
        append_event(
            agent_name,
            "stop",
            {"stop_hook_active": bool(payload.get("stop_hook_active"))},
            root=event_log_root,
        )
        # The Stop hook firing is the clean-turn-end signal. The success
        # status / reply summary were recorded on ``turn_context`` by the
        # conversation when it processed the ResultMessage (before Stop).
        await emit_completion_push(turn_context, push_fn, agent_name=agent_name)
        return {}

    return {
        "PreToolUse": [hook_matcher_cls(hooks=[_on_pretool])],
        "PostToolUse": [hook_matcher_cls(hooks=[_on_posttool])],
        "UserPromptSubmit": [hook_matcher_cls(hooks=[_on_prompt])],
        "Stop": [hook_matcher_cls(hooks=[_on_stop])],
    }


__all__ = ["TurnContext", "build_event_log_hooks", "emit_completion_push"]
