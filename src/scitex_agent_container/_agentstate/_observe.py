"""Take a real reading of one agent and return an :class:`.AgentState`.

The IO half. The rule (:mod:`._assess`) is pure and next door, so the rule is
testable without a fleet and this module is testable against a real one.

FOCUSED ON THE ONE PROBLEM: fleet agents stuck behind "Login expired" and not
recovering. The signals that serve it — ``is_login_required``, ``is_tmux_live``,
``is_process_alive``, ``is_at_idle_prompt`` — are observed here first-hand. The
remaining spec signals exist on the dataclass and are left ``None`` by this
observer, which is the honest value: this reader did not look at them, and a
``None`` says exactly that rather than implying health.

EVERY PROBE OBEYS THE SAME TWO CONTRACTS
    1. A probe that could not run yields ``None`` with a reason — never a pole.
       Blindness is not absence. From inside a container ``tmux ls`` SUCCEEDS and
       reports an empty fleet, and ``os.kill(host_pid, 0)`` raises for a live
       process; both are namespace blindness, and both must render ``None``.
    2. Whatever was read is kept VERBATIM in ``raw``. Not a tail, not a
       classification of it — the bytes. A tail slice is how an investigator
       watched a countdown widget change and nearly concluded "it is working"
       without ever seeing the content.

REUSE, NOT REINVENTION
    The two-capture frozen matcher is imported from the ``sac agents
    auth-status`` implementation (:mod:`.._runners._tmux.auth_status`) — the SAME
    matcher, so this reader and that command can never disagree about what a
    wedged pane looks like. Enumeration and capture come from the same command's
    tmux helpers, on the same server the live fleet runs on.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Sequence

from ._state import AgentState

__all__ = [
    "DEFAULT_INTERVAL",
    "observe_agent",
    "observe_fleet",
    "ps_line_for",
    "tui_pane_pid",
]

#: Seconds between the two pane captures. Same default as ``auth-status``.
DEFAULT_INTERVAL = 4.0

_TUI_PREFIX = "tui-"


def _in_sif() -> bool:
    """Are we blind to the host's tmux and pid namespace? Fails CAUTIOUS."""
    from .._lifecycle._verdict_tmux import in_sif

    return bool(in_sif())


def tui_pane_pid(session: str) -> int | None:
    """The pane's own pid for a tmux session, or ``None`` if we could not read it.

    The PANE pid, not the launcher's: the pane's ``bash -c`` ``exec``s the real
    process, and ``exec`` replaces the image while KEEPING the pid, so this is
    stable for the life of the session and is the value ``instances.pid`` records.
    """
    # stx-allow: fallback (reason: an unreadable pane pid must render None — a fabricated or guessed pid is strictly worse, since a recycled pid can vouch for a dead agent as alive)
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # stx-allow: fallback (reason: see above)
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    return int(text) if text.isdigit() else None


def ps_line_for(pid: int) -> tuple[str | None, str]:
    """``(raw ps line, detail)`` for ``pid``. ``None`` means we could not ask.

    Returns the RAW line INCLUDING THE START TIME (``lstart``), because "this pid
    exists" and "this pid is the process we started, not a recycled number" are
    different claims, and only the start time lets a later reader tell them
    apart. ``("", detail)`` is the positive observation of ABSENCE — ps ran and
    matched nothing — which is the only reading permitted to convict.
    """
    # stx-allow: fallback (reason: a ps we could not run observed NOTHING; None keeps that distinct from ps running and finding nothing, which is the only one of the two that may convict)
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,lstart=,stat=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # stx-allow: fallback (reason: see above)
        return None, f"could not run ps for pid {pid}: {type(exc).__name__}: {exc}"
    line = out.stdout.strip()
    if line:
        return line, f"ps matched pid {pid}"
    # ps exits 1 when nothing matched — that is a SUCCESSFUL probe reporting
    # absence, not a failed probe. Any other non-zero code is a failure to ask.
    if out.returncode in (0, 1):
        return "", f"ps ran and matched NO process for pid {pid}"
    return None, (
        f"ps exited {out.returncode} for pid {pid} — the probe did not answer, "
        f"which is not evidence that the process is gone"
    )


def _list_tui_sessions() -> list[str] | None:
    """Live ``tui-*`` sessions, or ``None`` when the probe was not a sensor.

    ``None`` covers BOTH ways this fails to see the fleet, and the second is the
    trap: from inside a container the probe does not error, it SUCCEEDS and
    reports an EMPTY fleet — true of the CONTAINER's ``/tmp``, and a false death
    verdict for every agent on the host.
    """
    if _in_sif():
        return None
    # stx-allow: fallback (reason: a tmux we could not ask observed nothing; None keeps "could not look" distinct from "looked and the fleet is empty")
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # stx-allow: fallback (reason: see above)
        return None
    if out.returncode != 0:
        return None
    return sorted(s for s in out.stdout.split() if s.startswith(_TUI_PREFIX))


def _capture(session: str) -> str | None:
    """One FULL pane capture, or ``None``. Reuses ``auth-status``'s capture."""
    from ..cli_pkg._auth_status import _capture as real_capture

    return real_capture(session)


def _pane_signals(
    state: AgentState, pane1: str | None, pane2: str | None
) -> AgentState:
    """Read ``is_login_required`` + ``is_at_idle_prompt`` from two captures.

    BOTH captures are required for a login verdict. The matcher's whole claim is
    that the banner stayed FROZEN across two reads — judging one capture against
    an empty prior would report "not stuck" for a pane nobody corroborated, which
    is a false GREEN on exactly the signal this feature exists to get right.
    """
    from .._runners._tmux.auth_status import evaluate, probe_to_state

    state = state.with_signal(
        "is_login_required",
        None,
        "",
        pane_run1=pane1 if pane1 is not None else "",
        pane_run2=pane2 if pane2 is not None else "",
    )

    if pane2 is None:
        return state.with_signal(
            "is_login_required",
            None,
            "the pane could not be captured — nothing was learned about this "
            "agent's auth, and an unread pane is not a healthy one",
        ).with_signal("is_at_idle_prompt", None, "the pane could not be captured")

    probe1, _ = evaluate(pane1, None)
    probe2, stuck = evaluate(pane2, probe_to_state(probe1))

    state = state.with_signal(
        "is_at_idle_prompt",
        bool(probe2.prompt_found),
        (
            "an input-prompt line was located on the pane"
            if probe2.prompt_found
            else "NO input-prompt line was found, so the near-prompt matcher had "
            "no anchor and its login reading is correspondingly weaker"
        ),
    )

    if pane1 is None:
        return state.with_signal(
            "is_login_required",
            None,
            "only ONE of the two captures could be read, so the FROZEN check "
            "could not run. A banner seen once may be moving — the agent working, "
            "or merely quoting the incident — and calling that healthy would be a "
            "false green on the exact signal this exists to get right",
        )

    if stuck:
        return state.with_signal(
            "is_login_required",
            True,
            f"a system auth banner sat directly above the prompt and stayed "
            f"FROZEN across both captures (banner={probe2.banner!r}, "
            f"distance={probe2.distance}) — corroborated wedge",
        )
    return state.with_signal(
        "is_login_required",
        False,
        (
            "a banner was present but MOVED between the two captures — the agent "
            "is producing output (working, or quoting the incident), not wedged"
            if probe2.present
            else "no system auth banner above the prompt in either capture"
        ),
    )


def observe_agent(
    name: str,
    *,
    interval: float = DEFAULT_INTERVAL,
    sessions: Sequence[str] | None = None,
    sessions_fn: Callable[[], list[str] | None] | None = None,
    capture_fn: Callable[[str], str | None] | None = None,
    pane_pid_fn: Callable[[str], int | None] | None = None,
    ps_fn: Callable[[int], tuple[str | None, str]] | None = None,
    now: float | None = None,
    observer: str = "sac agents state",
) -> AgentState:
    """Take a full reading of ONE agent. Always returns a state, never raises.

    ``sessions`` lets a fleet sweep pass the session list it already enumerated,
    so N agents cost ONE tmux enumeration rather than N. Every other collaborator
    is an injectable seam with a REAL default — the suite drives real panes, real
    processes and real files through these, never mocks.
    """
    state = AgentState(
        agent=name,
        observed_at=now if now is not None else time.time(),
        observer=observer,
    )
    session = f"{_TUI_PREFIX}{name}"

    # --- is_tmux_live --------------------------------------------------------
    listed = sessions if sessions is not None else (sessions_fn or _list_tui_sessions)()
    if listed is None:
        state = state.with_signal(
            "is_tmux_live",
            None,
            "the tmux session list could not be read as a SENSOR from here "
            "(tmux is wedged, or we are inside a container and the host's tmux "
            "socket is in another mount namespace — an 'empty fleet' seen from "
            "in there is blindness, not an empty fleet)",
            tmux_sessions="",
        )
    else:
        state = state.with_signal(
            "is_tmux_live",
            session in listed,
            (
                f"the tmux server lists {session}"
                if session in listed
                else f"the tmux server was read successfully and has NO "
                f"{session} session among its {len(listed)} tui- session(s)"
            ),
            tmux_sessions="\n".join(listed),
        )

    # --- is_process_alive (DECISIVE — so every non-observation must be None) --
    state = _process_signal(state, session, pane_pid_fn, ps_fn)

    # --- the pane, captured TWICE --------------------------------------------
    capture = capture_fn or _capture
    if state.is_tmux_live is False:
        return _pane_signals(state, None, None)
    pane1 = capture(session)
    time.sleep(max(0.0, interval))
    pane2 = capture(session)
    return _pane_signals(state, pane1, pane2)


def _process_signal(
    state: AgentState,
    session: str,
    pane_pid_fn: Callable[[str], int | None] | None,
    ps_fn: Callable[[int], tuple[str | None, str]] | None,
) -> AgentState:
    """``is_process_alive`` — the DECISIVE signal, so it convicts only first-hand.

    The bar is deliberately high, because a ``False`` here short-circuits past
    every other signal's UNKNOWN. It is reached ONLY when we are in the host's pid
    namespace, we obtained a real pane pid, and ``ps`` RAN and matched nothing.
    Every other outcome — a container, no pid, a ps that would not answer — is
    ``None``, because a pid read across a namespace boundary is not a weak sensor,
    it is NOT A SENSOR.
    """
    if _in_sif():
        return state.with_signal(
            "is_process_alive",
            None,
            "we are INSIDE a container, so the pane pid lives in the HOST's pid "
            "namespace and cannot be read from here. A decisive signal may never "
            "be taken across a namespace boundary",
        )
    pid = (pane_pid_fn or tui_pane_pid)(session)
    if pid is None:
        return state.with_signal(
            "is_process_alive",
            None,
            f"no pane pid could be read for {session}, so there was no process to "
            f"look for. That is NOT an observation that the process is gone",
            pane_pid="",
        )
    line, detail = (ps_fn or ps_line_for)(pid)
    if line is None:
        return state.with_signal(
            "is_process_alive", None, detail, pane_pid=str(pid), ps_line=""
        )
    if line == "":
        return state.with_signal(
            "is_process_alive",
            False,
            f"{detail} — a DIRECT observation of absence from the process table, "
            f"which is what makes this signal decisive",
            pane_pid=str(pid),
            ps_line="",
        )
    return state.with_signal(
        "is_process_alive", True, detail, pane_pid=str(pid), ps_line=line
    )


def observe_fleet(
    names: Sequence[str],
    *,
    interval: float = DEFAULT_INTERVAL,
    sessions_fn: Callable[[], list[str] | None] | None = None,
    **kwargs,
) -> list[AgentState]:
    """Read EVERY name in the roster — one row each, including the missing ones.

    The roster is the POPULATION; the tmux enumeration is only a READING of it.
    An agent absent from the reading still gets a row here, because the enumerate-
    and-report shape is what made absence invisible: an agent that never became a
    dict key could not be reported as anything, so a wedged agent produced no line
    and the silence read as a healthy fleet.

    The session list is enumerated ONCE and shared, so the two captures per agent
    are the only per-agent tmux cost.
    """
    listed = (sessions_fn or _list_tui_sessions)()
    return [
        observe_agent(name, interval=interval, sessions=listed, **kwargs)
        for name in names
    ]


# EOF
