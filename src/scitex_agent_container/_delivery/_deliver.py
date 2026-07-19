"""The verified send — ONE verb, two strategies, a tri-state answer either way.

This module is WIRING, deliberately. Every hard part already existed somewhere in
this repo and was simply never composed into a single answerable question:

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
* :mod:`.._runners._tmux.auth_status` — the near-prompt auth-banner matcher.

WHY THE TRAILING ENTER DID NOT TAKE
-----------------------------------
The operator observed a message land in a peer's composer while the agent stayed
idle, and a bare Enter into that pane started it working immediately. The repo had
already root-caused this once, on the boot path, under card
``sac-tui-enter-drop-on-boot``, and the answer is BOTH of the suspected causes at
once:

1. the Ink TUI drops NON-LITERAL ``send-keys``, so the text must be pasted with
   ``-l`` and the submit must be a SEPARATE named ``Enter`` (never ``-l``, which
   would type the five characters "Enter"); and
2. the Ink TUI eats an ``Enter`` fired while the pane is BUSY (spinner up, MCP
   reconnecting, a hook running) — and a ``UserPromptSubmit`` hook alone can hold
   that window for 30 seconds.

A blind ``send-keys text Enter`` loses the submit to (2) whenever the peer happens
to be working, which is most of the time on a busy fleet. The fix is not a longer
sleep: it is to WAIT FOR IDLE, send exactly one Enter, VERIFY the compose buffer
advanced, and retry bounded — which is what ``verify_submit_by_advancement``
already does. This module reuses it rather than reimplementing it, so the boot
path and the a2a path can never drift apart on the one behaviour that was hardest
to get right.

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

from ._route import STRATEGY_SDK, STRATEGY_TUI, Route, resolve_route
from ._state import DeliveryState
from ._token import format_payload, make_token, pane_contains_token

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

#: How long to wait for the pane to go idle before each submit attempt. Sized
#: for a peer that is genuinely working: a UserPromptSubmit hook alone can take
#: 30s, so anything under a minute manufactures false "wedged" readings.
DEFAULT_IDLE_WAIT_S = 60.0

#: Bounded submit retries. Each one re-checks idle from scratch, so this is a
#: budget of ATTEMPTS, not a burst of blind Enters (the burst was the old bug).
DEFAULT_MAX_RESENDS = 8

#: Poll cadence for every wait loop here.
DEFAULT_POLL_S = 0.6

OBSERVER = "sac agents deliver"


class _CaptureTap:
    """A real capture callable that counts unreadable captures. Not a mock.

    ``verify_submit_by_advancement`` is typed ``capture_fn(name) -> str`` and has
    no way to express "the pane could not be read", so this adapter turns a
    ``None`` into ``""`` for it while REMEMBERING that it happened. Without the
    memory, a submit failure caused by blindness would be indistinguishable from
    one caused by a dropped Enter, and only the second of those is a refutation.
    The tap is what lets the caller downgrade a ``False`` to ``None`` when any
    part of the observation window was blind.
    """

    def __init__(self, capture_fn: Callable[[str], Optional[str]]) -> None:
        self._capture_fn = capture_fn
        self.unreadable = 0
        self.readable = 0
        self.last_readable = ""

    def read(self, target: str) -> Optional[str]:
        """Capture, recording readability, and preserve the tri-state."""
        pane = self._capture_fn(target)
        if pane is None:
            self.unreadable += 1
        else:
            self.readable += 1
            self.last_readable = pane
        return pane

    def __call__(self, target: str) -> str:
        """The ``capture_fn(name) -> str`` shape the submit verifier requires."""
        return self.read(target) or ""


def _default_capture(session: str) -> Optional[str]:
    """The fleet's correct pane read: default server, ``-J``, ``None`` on error."""
    from ..cli_pkg._auth_status import _capture

    return _capture(session)


def _default_paste(session: str, text: str) -> None:
    """Literal paste, no submit. ``-l`` is not optional — see the module docstring."""
    from .._runners._tmux.tmux import TmuxManager

    TmuxManager.send_text_literal(session, text)


def _default_send_keys(session: str, key: str) -> None:
    from .._runners._tmux.tmux import TmuxManager

    TmuxManager.send_keys(session, key)


def _default_sdk_send(agent: str, payload: str) -> tuple[Optional[bool], str]:
    """Deliver through the existing ``sac agents send`` library path.

    ``wait=True`` on purpose. The non-blocking default returns
    ``status="dispatched"``, which means "the agent looks reachable and here is a
    command YOU can run to actually send it" — validation, not delivery. Reading
    that as a delivered message would reintroduce the exact bug this package
    exists to remove, one layer up.
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
            "RUNNING — a timeout is a statement about our patience, not about "
            "delivery, and must not be recorded as a failed send"
        )
    return None, (
        f"send_to_agent returned an unrecognised status {status!r}; refusing to "
        f"guess which pole it means"
    )


def _observe_pane_before(state: DeliveryState, pane: Optional[str]) -> DeliveryState:
    """Record what the pane looked like BEFORE the send: readable / busy / banner."""
    from .._lifecycle.liveness_probe import pane_is_busy

    if pane is None:
        state = state.with_signal(
            "is_pane_readable",
            False,
            "the pre-send capture failed — the session may have vanished between "
            "the enumeration and the capture",
        )
        return state.with_signal(
            "is_target_busy_before",
            None,
            "not read: the pane could not be captured",
        ).with_signal(
            "is_login_banner_before",
            None,
            "not read: the pane could not be captured",
        )

    from .._runners._tmux.auth_status import evaluate

    state = state.with_signal(
        "is_pane_readable", True, "the pre-send pane was captured", pane_before=pane
    )
    busy = pane_is_busy(pane)
    state = state.with_signal(
        "is_target_busy_before",
        busy,
        (
            "an in-progress marker was on the pane tail — the peer is WORKING, "
            "which is healthy; the submit step will wait for it to go idle"
            if busy
            else "no in-progress marker on the pane tail"
        ),
    )
    probe, _ = evaluate(pane, None)
    return state.with_signal(
        "is_login_banner_before",
        bool(probe.present),
        (
            "an auth banner sits near the prompt on this SINGLE capture. "
            "UNCORROBORATED — run `sac agents auth-status` for the two-capture "
            "frozen check before acting. If it is real, a submitted turn will "
            "produce nothing and resending will not help"
            if probe.present
            else "no auth banner near the prompt"
        ),
    )


def _watch_for_arrival(
    session: str,
    token: str,
    *,
    tap: _CaptureTap,
    timeout_s: float,
    poll_s: float,
    time_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[Optional[bool], str, str]:
    """Poll until the token renders. Returns ``(signal, reason, last_pane)``.

    Tri-state: ``True`` on a match, ``False`` when readable captures were taken
    throughout and none contained it, and ``None`` when NO readable capture was
    ever taken — because a window in which we never once saw the screen cannot
    support a claim about what was on it.
    """
    deadline = time_fn() + timeout_s
    last = ""
    while True:
        pane = tap.read(session)
        if pane is not None:
            last = pane
        if pane_contains_token(pane, token) is True:
            return (
                True,
                f"token {token} was found on the pane after the paste",
                last,
            )
        if time_fn() >= deadline:
            break
        if poll_s > 0:
            sleep_fn(poll_s)
    if tap.readable == 0:
        return (
            None,
            f"the pane could not be read even once in {timeout_s:.0f}s, so "
            f"nothing is known about whether the payload arrived",
            last,
        )
    return (
        False,
        f"token {token} did not appear on {tap.readable} readable capture(s) "
        f"over {timeout_s:.0f}s. NOTE the matcher flattens the pane before "
        f"searching, so this is not a wrapping artefact",
        last,
    )


def deliver(
    agent: str,
    message: str,
    *,
    strategy: str = "auto",
    arrival_timeout_s: float = DEFAULT_ARRIVAL_TIMEOUT_S,
    idle_wait_s: float = DEFAULT_IDLE_WAIT_S,
    max_resends: int = DEFAULT_MAX_RESENDS,
    poll_s: float = DEFAULT_POLL_S,
    capture_fn: Callable[[str], Optional[str]] = _default_capture,
    paste_fn: Callable[[str, str], None] = _default_paste,
    send_keys_fn: Callable[[str, str], None] = _default_send_keys,
    sdk_send_fn: Callable[[str, str], tuple[Optional[bool], str]] = _default_sdk_send,
    list_sessions_fn: Optional[Callable[[], Optional[list[str]]]] = None,
    session_id_fn: Optional[Callable[[str], Optional[str]]] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.time,
) -> DeliveryState:
    """Send ``message`` to ``agent`` and RETURN WHAT IS KNOWN ABOUT THE OUTCOME.

    Every collaborator is an injected seam with a REAL production default, so a
    test drives this function itself — not a rehearsal of it — by passing plain
    objects with the same signatures.

    The sequence, and what each step buys:

    1. **Resolve and prove the target exists** (:func:`.._route.resolve_route`).
       A missing session is a distinct, loud outcome, not a silent success.
    2. **Observe the pane BEFORE sending** — readable, busy, auth banner. This is
       evidence, never a veto: a busy peer is a healthy peer and delivery to it
       works fine.
    3. **Paste literally**, with a short token injected at the front.
    4. **Confirm arrival by token**, against a FLATTENED pane so a re-render
       cannot fake a negative.
    5. **Confirm SUBMISSION** and retry the Enter — the step that was missing,
       via the already-proven ``verify_submit_by_advancement``.
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

    state = state.with_signal(
        "is_route_resolved",
        route.resolved,
        route.reason,
        tmux_sessions=route.sessions_raw,
    )
    state = replace(state, strategy=route.strategy)

    if route.resolved is not True:
        return _finish(_no_attempt(state, route), started, time_fn)
    if route.strategy == STRATEGY_SDK:
        return _finish(
            _deliver_via_sdk(state, agent, payload, sdk_send_fn), started, time_fn
        )
    return _finish(
        _deliver_via_tui(
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

    Subtle but load-bearing: when the route did not resolve, ``False`` is the
    honest value for both payload signals, because we know with certainty that
    nothing was transmitted — this is complete information, not a guess. The
    verdict then lands on ``False`` and the exit code refines to
    ``EXIT_NO_ROUTE``. When the route is UNKNOWN (a blind enumeration) the route
    signal itself is ``None``, so the fold returns UNKNOWN regardless of what
    these two say — blindness cannot be argued into a conviction.
    """
    why = (
        "nothing was transmitted — the route did not resolve. This is a KNOWN "
        "negative (we can be certain no bytes were sent), not an unread signal"
    )
    return state.with_signal("is_payload_delivered", False, why).with_signal(
        "is_payload_submitted", False, why
    )


def _deliver_via_sdk(
    state: DeliveryState,
    agent: str,
    payload: str,
    sdk_send_fn: Callable[[str, str], tuple[Optional[bool], str]],
) -> DeliveryState:
    """The existing send path. One call answers both payload signals.

    A completed turn is proof of BOTH arrival and submission — the SDK path has
    no composer to leave text sitting in, so the mode that motivates this whole
    package cannot occur here. ``is_pane_readable`` stays ``None``: there is no
    pane, and "nothing to read" is not "failed to read".
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


def _deliver_via_tui(
    state: DeliveryState,
    route: Route,
    payload: str,
    token: str,
    *,
    capture_fn: Callable[[str], Optional[str]],
    paste_fn: Callable[[str, str], None],
    send_keys_fn: Callable[[str, str], None],
    arrival_timeout_s: float,
    idle_wait_s: float,
    max_resends: int,
    poll_s: float,
    time_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> DeliveryState:
    """Paste literally, confirm arrival by token, then confirm SUBMISSION."""
    from ..runtimes._tui_compose import verify_submit_by_advancement

    session = route.session
    state = _observe_pane_before(state, capture_fn(session))

    paste_fn(session, payload)

    tap = _CaptureTap(capture_fn)
    arrived, why, pane_after = _watch_for_arrival(
        session,
        token,
        tap=tap,
        timeout_s=arrival_timeout_s,
        poll_s=poll_s,
        time_fn=time_fn,
        sleep_fn=sleep_fn,
    )
    state = state.with_signal(
        "is_payload_delivered", arrived, why, pane_after_paste=pane_after
    )

    blind_before = tap.unreadable
    submitted = verify_submit_by_advancement(
        session,
        capture_fn=tap,
        send_keys_fn=lambda key: send_keys_fn(session, key),
        max_resends=max_resends,
        poll_s=poll_s,
        idle_wait_s=idle_wait_s,
        sleep_fn=sleep_fn,
        time_fn=time_fn,
    )
    blind_during = tap.unreadable - blind_before

    if submitted:
        value: Optional[bool] = True
        reason = (
            "the live compose box was observed to CLEAR after an idle-gated "
            "Enter — the turn was submitted"
        )
    elif blind_during:
        value = None
        reason = (
            f"the submit could not be confirmed, but {blind_during} capture(s) "
            f"during the attempt were unreadable. A submit failure observed "
            f"through a partly blind window is not a refutation"
        )
    else:
        value = False
        reason = (
            f"the payload is STILL SITTING UNSENT in the compose box after "
            f"{max_resends} idle-gated Enter attempts. Do NOT resend — the text "
            f"is already there and a second copy would stack on it. Attach and "
            f"press Enter: `tmux attach -t {session}`"
        )
    return state.with_signal(
        "is_payload_submitted", value, reason, pane_after_submit=tap.last_readable
    )


def _finish(
    state: DeliveryState, started: float, time_fn: Callable[[], float]
) -> DeliveryState:
    """Stamp the elapsed time so a budget can be tuned against measurements."""
    return replace(state, elapsed=time_fn() - started)


# EOF
