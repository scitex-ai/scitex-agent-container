"""Strategy 2 — the VERIFIED tmux path, the only one that reaches a TUI agent.

Paste literally, confirm arrival by token, then confirm SUBMISSION and retry the
Enter. Each step is an existing, proven primitive; this module is the wiring plus
the tri-state bookkeeping those primitives cannot express on their own.

WHY THE TRAILING ENTER DID NOT TAKE
-----------------------------------
The operator saw a message land in a peer's composer while the agent stayed idle,
and a bare Enter into that pane started it working immediately. The repo had
already root-caused this once on the boot path (card ``sac-tui-enter-drop-on-boot``)
and the answer is BOTH suspected causes at once:

1. the Ink/React TUI silently DROPS non-literal ``send-keys``, so the text must go
   in with ``-l`` and the submit must be a SEPARATE named ``Enter`` — never ``-l``,
   which would type the five characters "Enter" into the box; and
2. the TUI EATS an ``Enter`` fired while the pane is BUSY (spinner up, MCP
   reconnecting, a hook running), and a ``UserPromptSubmit`` hook alone can hold
   that window for 30 seconds.

A blind ``send-keys text Enter`` loses the submit to (2) whenever the peer happens
to be working, which on a busy fleet is most of the time. The fix is not a longer
sleep — it is to WAIT FOR IDLE, send exactly one Enter, VERIFY the compose buffer
advanced, and retry bounded. That is precisely
:func:`..runtimes._tui_compose.verify_submit_by_advancement`, so this module calls
it rather than growing a second copy that could drift from the boot path on the
one behaviour that was hardest to get right.
"""

from __future__ import annotations

from typing import Callable, Optional

from ._route import Route
from ._state import DeliveryState
from ._token import pane_contains_token

__all__ = [
    "CaptureTap",
    "default_capture",
    "default_paste",
    "default_send_keys",
    "deliver_via_tui",
    "observe_pane_before",
]


class CaptureTap:
    """A real capture callable that REMEMBERS how often it could not read.

    ``verify_submit_by_advancement`` is typed ``capture_fn(name) -> str`` and has
    no way to express "the pane could not be read", so this adapter substitutes
    ``""`` for it while recording that a substitution happened. Without that
    memory, a submit outcome computed against a blind window would be
    indistinguishable from one computed against a screen we actually saw — and
    only the second kind may become a verdict.
    """

    def __init__(self, capture_fn: Callable[[str], Optional[str]]) -> None:
        self._capture_fn = capture_fn
        self.unreadable = 0
        self.readable = 0
        self.last_readable = ""

    def read(self, target: str) -> Optional[str]:
        """Capture, recording readability, and PRESERVE the tri-state."""
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


def default_capture(session: str) -> Optional[str]:
    """The fleet's CORRECT pane read: default server, ``-J``, ``None`` on error.

    Deliberately ``cli_pkg._auth_status._capture`` and not
    ``_runners._tmux.pane_capture``: the latter targets a DEDICATED tmux server
    named ``sac``, not the default server the live fleet runs on, so it would
    report a different server's emptiness as this agent's death. ``-J`` rejoins
    tmux's own soft wraps, and ``None`` on any error keeps "uncapturable"
    distinct from "clean pane".
    """
    from ..cli_pkg._auth_status import _capture

    return _capture(session)


def default_paste(session: str, text: str) -> None:
    """Literal paste, NO submit. The ``-l`` is not optional — see the docstring."""
    from .._runners._tmux.tmux import TmuxManager

    TmuxManager.send_text_literal(session, text)


def default_send_keys(session: str, key: str) -> None:
    """A named key (``Enter``), sent separately from the literal text."""
    from .._runners._tmux.tmux import TmuxManager

    TmuxManager.send_keys(session, key)


def observe_pane_before(state: DeliveryState, pane: Optional[str]) -> DeliveryState:
    """Record what the pane looked like BEFORE the send: readable / busy / banner.

    All three are EVIDENCE, never a veto. A busy peer is a working peer and
    delivery to it succeeds normally; a banner seen on one capture is
    uncorroborated. Refusing to send on either would block exactly the agents
    that are fine.
    """
    from .._lifecycle.liveness_probe import pane_is_busy

    if pane is None:
        return (
            state.with_signal(
                "is_pane_readable",
                False,
                "the pre-send capture failed — the session may have vanished "
                "between the enumeration and the capture",
            )
            .with_signal(
                "is_target_busy_before",
                None,
                "not read: the pane could not be captured",
            )
            .with_signal(
                "is_login_banner_before",
                None,
                "not read: the pane could not be captured",
            )
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
    tap: CaptureTap,
    timeout_s: float,
    poll_s: float,
    time_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> tuple[Optional[bool], str, str]:
    """Poll until the token renders. Returns ``(signal, reason, last_pane)``.

    Tri-state: ``True`` on a match, ``False`` when readable captures were taken
    and none contained it, and ``None`` when NO readable capture was ever taken —
    a window in which we never once saw the screen cannot support a claim about
    what was on it.
    """
    deadline = time_fn() + timeout_s
    last = ""
    while True:
        pane = tap.read(session)
        if pane is not None:
            last = pane
        if pane_contains_token(pane, token) is True:
            return True, f"token {token} was found on the pane after the paste", last
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
        f"searching, so this is NOT a wrapping artefact",
        last,
    )


def _submission_signal(
    *,
    submitted: bool,
    readable_during: int,
    blind_during: int,
    max_resends: int,
    session: str,
) -> tuple[Optional[bool], str]:
    """Turn the verifier's bool into a TRI-STATE, guarding its vacuous True.

    THE ORDER OF THESE BRANCHES IS THE POINT, and it was wrong once. A wholly
    blind window is checked FIRST, before ``submitted`` is consulted at all,
    because ``verify_submit_by_advancement`` returns ``True`` from its phase 1
    when the compose box never showed pending text — "there is nothing to force".
    Against a pane that could not be read, every capture it saw was the empty
    string :class:`CaptureTap` substitutes for ``None``, so that ``True`` was
    computed from evidence WITH NO WAY TO DISAGREE WITH IT. Trusting it would
    report a successful submission to an agent whose screen we never saw. The
    verifier is not wrong; it was asked a question it could not know it was
    unable to answer, so the guard belongs here.

    A PARTIALLY blind window is treated asymmetrically on purpose: it may still
    carry a positive (we watched the buffer clear with our own eyes) but never a
    negative (a failure seen through gaps is not a refutation).
    """
    if readable_during == 0:
        return None, (
            f"not one readable capture was taken during the submit attempt "
            f"({blind_during} unreadable). The submit verifier returned "
            f"{submitted!r}, but it read only empty strings, so that answer is "
            f"vacuous and is NOT recorded as a verdict"
        )
    if submitted:
        return True, (
            "the live compose box was observed to CLEAR after an idle-gated "
            "Enter — the turn was submitted"
        )
    if blind_during:
        return None, (
            f"the submit could not be confirmed, but {blind_during} capture(s) "
            f"during the attempt were unreadable. A submit failure observed "
            f"through a partly blind window is not a refutation"
        )
    return False, (
        f"the payload is STILL SITTING UNSENT in the compose box after "
        f"{max_resends} idle-gated Enter attempts. Do NOT resend — the text is "
        f"already there and a second copy would stack on it. Attach and press "
        f"Enter: `tmux attach -t {session}`"
    )


def deliver_via_tui(
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
    """Observe, paste literally, confirm arrival by token, then confirm SUBMISSION."""
    from ..runtimes._tui_compose import verify_submit_by_advancement

    session = route.session
    state = observe_pane_before(state, capture_fn(session))

    paste_fn(session, payload)

    tap = CaptureTap(capture_fn)
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
    readable_before = tap.readable
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
    value, reason = _submission_signal(
        submitted=bool(submitted),
        readable_during=tap.readable - readable_before,
        blind_during=tap.unreadable - blind_before,
        max_resends=max_resends,
        session=session,
    )
    return state.with_signal(
        "is_payload_submitted", value, reason, pane_after_submit=tap.last_readable
    )


# EOF
