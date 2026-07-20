"""Strategy 1 — deliver through the EXISTING ``sac agents send`` machinery.

The preferred path whenever the target has a recorded session id, because it is
already written, already used, and has no composer for text to sit unsent in. Its
whole job here is to answer the same two signals the TUI strategy answers, so one
verb can return one shape regardless of how the message travelled.

Measured on the live host: only a handful of agents have a ``session_id`` file at
all. That is why this cannot be the only strategy, and why a caller who reads
"``send`` failed" against a TUI agent is reading the wrong thing — the path was
never able to reach it.
"""

from __future__ import annotations

from typing import Callable, Optional

from ._state import DeliveryState

__all__ = ["default_sdk_send", "deliver_via_sdk"]


def default_sdk_send(agent: str, payload: str) -> tuple[Optional[bool], str]:
    """Deliver through the ``sac agents send`` library sibling.

    ``wait=True`` on purpose. The non-blocking default returns
    ``status="dispatched"``, which means "the agent looks reachable, and here is a
    command YOU can run to actually send it" — validation, NOT delivery. Recording
    that as a delivered message would reintroduce the exact bug this package
    exists to remove, one layer up: an instrument reporting good news about a
    thing it never observed.
    """
    # stx-allow: fallback (reason: a transport failure must render UNKNOWN, not a
    # refutation — we cannot tell a refused send from an unobserved one)
    try:
        from ..cli_pkg._send import send_to_agent

        result = send_to_agent(agent, payload, wait=True)
    except Exception as exc:  # stx-allow: fallback (reason: see comment above)
        return None, (
            f"send_to_agent raised {type(exc).__name__}: {exc}. A transport "
            f"failure says nothing about whether the turn reached the agent"
        )
    status = str(result.get("status") or "")
    if status == "ok":
        return True, "send_to_agent completed the turn (status='ok')"
    if status in ("error", "creds-expired"):
        return False, (
            f"send_to_agent refused the turn (status={status!r}): "
            f"{result.get('error') or 'no detail'}"
        )
    if status == "timeout":
        return None, (
            "send_to_agent timed out waiting for the reply. The turn may well be "
            "RUNNING — a timeout is a statement about OUR PATIENCE, not about "
            "delivery, and must never be recorded as a failed send"
        )
    if status == "dispatched":
        return None, (
            "send_to_agent returned status='dispatched', which validates "
            "reachability WITHOUT delivering. Nothing was actually sent to the "
            "agent, so no delivery claim can be made from it"
        )
    return None, (
        f"send_to_agent returned an unrecognised status {status!r}; refusing to "
        f"guess which pole it means"
    )


def deliver_via_sdk(
    state: DeliveryState,
    agent: str,
    payload: str,
    sdk_send_fn: Callable[[str, str], tuple[Optional[bool], str]],
) -> DeliveryState:
    """One call answers BOTH payload signals.

    A completed turn is proof of arrival AND of submission — this path has no
    compose box for text to sit unsent in, so the mode that motivates the whole
    package cannot occur here. ``is_pane_readable`` is deliberately left ``None``:
    there is no pane, and "there was nothing to read" is not "we failed to read
    it". Spelling it ``False`` would invent a failure out of an inapplicable
    question.
    """
    ok, detail = sdk_send_fn(agent, payload)
    return state.with_signal(
        "is_payload_delivered", ok, detail, send_detail=detail
    ).with_signal(
        "is_payload_submitted",
        ok,
        f"{detail} (on the SDK path a completed turn proves submission — there "
        f"is no composer for text to sit unsent in)",
    )


# EOF
