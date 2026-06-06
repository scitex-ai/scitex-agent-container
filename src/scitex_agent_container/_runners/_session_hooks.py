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
    * the push would be structurally empty (see the fleet-noise guards
      inline below): the summary is empty/whitespace AND either there is
      no ledger correlation (``dispatch_id is None``) OR no honest
      outcome was recorded (``status == STATUS_UNKNOWN``). Every empty
      push burns the requester's turn cycle for zero signal; the union
      catches both noise patterns observed in the wild without dropping
      any signal-carrying push.

    Status is taken verbatim from ``turn_context.status`` — set honestly by
    the conversation (``success`` on clean drain, ``failure`` on SDK error).
    A ``None`` status (the push fired before the conversation recorded an
    outcome — should not happen, since Stop comes after the ResultMessage)
    is reported as ``unknown``, NEVER fabricated as success.

    A delivery failure is LOUD (logged at WARNING, requester named) but
    never re-raised: the turn already completed, and a flaky receipt must
    not crash the agent or abort the SDK control flow.
    """
    if turn_context is None or push_fn is None:
        return
    requester = turn_context.requester
    if not requester or turn_context.pushed:
        return

    from ._session_completion import STATUS_UNKNOWN, build_completion_report

    status = turn_context.status or STATUS_UNKNOWN
    summary_stripped = (turn_context.summary or "").strip()

    # Fleet-noise guards — suppress structurally-empty pings at the
    # source. Either signature flips the noise gate:
    #
    # (1) PR #309 (2026-06-05): no ledger correlation AND no content.
    #     The wire signature
    #     ``{"agent": <name>, "dispatch_id": null, "status": "unknown",
    #     "summary": ""}`` is the canonical "nothing real to report"
    #     payload — every idle/wake turn that had no incoming
    #     dispatch_id and produced no assistant text used to fire one.
    #
    # (2) Lead 2026-06-07 (conv 62b0f613104048c6adae11c73d9f8b63): no
    #     honest outcome AND no content, REGARDLESS of dispatch_id. A
    #     push with ``status:"unknown"`` + empty summary carries no
    #     information for the requester even when a dispatch_id is
    #     set — "we tried this dispatch and have nothing to say about
    #     it" is worse than useless. Observed in the wild: roughly 1
    #     in 6 empty pings from a stuck sibling rode a real
    #     dispatch_id and leaked past guard (1).
    #
    # Signal preserved: a push with non-empty summary always emits; a
    # push with empty summary but an honest status (``success`` or
    # ``failure``) AND a dispatch_id still emits so the requester can
    # correlate. ``pushed = True`` still flips so a re-entrant Stop
    # hook on the same turn re-evaluates and skips identically.
    if not summary_stripped and (
        turn_context.dispatch_id is None or status == STATUS_UNKNOWN
    ):
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
