"""ONE verb, TWO strategies — the orchestrator, and nothing else.

This module is WIRING, deliberately. Every hard part already existed in this repo
and was simply never composed into a single answerable question:

* :mod:`.._lifecycle.liveness_probe` — the nonce generator and the busy-marker
  classifier (the fleet's SSOT for "is this pane mid-turn?");
* :mod:`..runtimes._tui_compose` — ``verify_submit_by_advancement``, the
  idle-gated Enter with bounded retry that is the ALREADY-DIAGNOSED fix for the
  unsubmitted-composer mode;
* :mod:`.._runners._tmux.tmux` — ``send_text_literal``, whose ``-l`` flag is
  REQUIRED because the containerized Ink/React TUI silently drops non-literal
  ``send-keys``;
* :mod:`..cli_pkg._auth_status` — ``_capture``, which returns ``None`` on any
  error so "uncapturable" stays distinct from "clean pane";
* :mod:`..cli_pkg._send` — the existing send path, preferred whenever it applies.

The per-strategy work lives in :mod:`._sdk_strategy` and :mod:`._tui_strategy`.
What stays here is the sequence and the one decision that binds them: resolve the
route FIRST, and let the route pick the strategy.

BUDGETS ARE GENEROUS AND CONFIGURABLE
-------------------------------------
Every default below is deliberately larger than feels necessary, because the
recorded failure was a 25-second wait misread as death. Waiting too long costs
seconds; concluding death too early costs a restart that destroys a working
agent's context.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from ._route import STRATEGY_SDK, Route, resolve_route
from ._sdk_strategy import default_sdk_send, deliver_via_sdk
from ._state import DeliveryState
from ._token import format_payload, make_token
from ._tui_strategy import (
    default_capture,
    default_paste,
    default_send_keys,
    deliver_via_tui,
)

__all__ = [
    "DEFAULT_ARRIVAL_TIMEOUT_S",
    "DEFAULT_IDLE_WAIT_S",
    "DEFAULT_MAX_RESENDS",
    "DEFAULT_POLL_S",
    "OBSERVER",
    "deliver",
]

#: How long to watch for the token to RENDER after the paste. The paste is
#: instant; the render is not, and a multi-line payload takes a beat.
DEFAULT_ARRIVAL_TIMEOUT_S = 30.0

#: How long to wait for the pane to go idle before each submit attempt. Sized for
#: a peer that is genuinely working: a UserPromptSubmit hook alone can take 30s,
#: so anything under a minute manufactures false "wedged" readings.
DEFAULT_IDLE_WAIT_S = 60.0

#: Bounded submit retries. Each one re-checks idle from scratch, so this is a
#: budget of ATTEMPTS, not a burst of blind Enters (the burst was the old bug).
DEFAULT_MAX_RESENDS = 8

#: Poll cadence for every wait loop here.
DEFAULT_POLL_S = 0.6

OBSERVER = "sac agents deliver"


def deliver(
    agent: str,
    message: str,
    *,
    strategy: str = "auto",
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
    idle_wait_s: float = DEFAULT_IDLE_WAIT_S,
    max_resends: int = DEFAULT_MAX_RESENDS,
    poll_s: float = DEFAULT_POLL_S,
    capture_fn: Callable[[str], Optional[str]] = default_capture,
    paste_fn: Callable[[str, str], None] = default_paste,
    send_keys_fn: Callable[[str, str], None] = default_send_keys,
    sdk_send_fn: Callable[[str, str], tuple[Optional[bool], str]] = default_sdk_send,
    list_sessions_fn: Optional[Callable[[], Optional[list[str]]]] = None,
    session_id_fn: Optional[Callable[[str], Optional[str]]] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.time,
) -> DeliveryState:
    """Send ``message`` to ``agent`` and RETURN WHAT IS KNOWN ABOUT THE OUTCOME.

    Every collaborator is an injected seam with a REAL production default, so a
    test drives this function itself — not a rehearsal of it — by passing plain
    objects carrying the same signatures.

    The sequence, and what each step buys:

    1. **Resolve and prove the target exists** (:func:`._route.resolve_route`).
       A missing session becomes a distinct, loud outcome instead of a silent
       success, and a BLIND enumeration becomes UNKNOWN instead of a death
       sentence.
    2. **Observe the pane BEFORE sending** — readable, busy, auth banner. All
       evidence, never a veto: a busy peer is a healthy peer.
    3. **Paste literally**, with a short token injected at the front.
    4. **Confirm arrival by token**, against a FLATTENED pane so a re-render
       cannot fake a negative.
    5. **Confirm SUBMISSION** and retry the Enter — the step that was missing.
    """
    started = time_fn()
    token = make_token()
    payload = format_payload(message, token)
    state = DeliveryState(
        agent=agent,
        token=token,
        observed_at=clock_fn(),
        observer=OBSERVER,
    )

    route_kwargs: dict = {"strategy": strategy}
    if list_sessions_fn is not None:
        route_kwargs["list_sessions_fn"] = list_sessions_fn
    if session_id_fn is not None:
        route_kwargs["session_id_fn"] = session_id_fn
    route = resolve_route(agent, **route_kwargs)

    state = replace(
        state.with_signal(
            "is_route_resolved",
            route.resolved,
            route.reason,
            tmux_sessions=route.sessions_raw,
        ),
        strategy=route.strategy,
    )

    if route.resolved is not True:
        return _finish(_no_attempt(state, route), started, time_fn)
    if route.strategy == STRATEGY_SDK:
        return _finish(
            deliver_via_sdk(state, agent, payload, sdk_send_fn), started, time_fn
        )
    return _finish(
        deliver_via_tui(
            state,
            route,
            payload,
            token,
            capture_fn=capture_fn,
            paste_fn=paste_fn,
            send_keys_fn=send_keys_fn,
            arrival_timeout_s=arrival_timeout_s,
            idle_wait_s=idle_wait_s,
            max_resends=max_resends,
            poll_s=poll_s,
            time_fn=time_fn,
            sleep_fn=sleep_fn,
        ),
        started,
        time_fn,
    )


def _no_attempt(state: DeliveryState, route: Route) -> DeliveryState:
    """Nothing was sent. Say so as a KNOWN negative, not as an unread signal.

    Subtle but load-bearing. When the route did not resolve, ``False`` is the
    HONEST value for both payload signals, because we know with certainty that
    nothing was transmitted — this is complete information, not a guess. The fold
    then lands on ``False`` and the exit code refines to ``EXIT_NO_ROUTE``.

    When the route is UNKNOWN (a blind enumeration) the route signal itself is
    ``None``, so the fold returns UNKNOWN regardless of what these two say.
    Blindness cannot be argued into a conviction, and it is the route signal —
    not these — that carries that distinction.
    """
    del route
    why = (
        "nothing was transmitted — the route did not resolve. This is a KNOWN "
        "negative (we can be certain no bytes were sent), not an unread signal"
    )
    return state.with_signal("is_payload_delivered", False, why).with_signal(
        "is_payload_submitted", False, why
    )


def _finish(
    state: DeliveryState, started: float, time_fn: Callable[[], float]
) -> DeliveryState:
    """Stamp the elapsed time so a budget can be tuned against measurements.

    The 25-second window that was misread as death is exactly the number this
    makes visible — a budget argued from a measurement beats one argued from a
    memory of how long it felt.
    """
    return replace(state, elapsed=time_fn() - started)


# EOF
