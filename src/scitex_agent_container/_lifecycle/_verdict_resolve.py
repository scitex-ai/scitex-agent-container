"""Gather the real liveness signals and fold them into a :mod:`._verdict`.

This is the verdict's IO and nothing else — the pure decision rule lives next
door in :mod:`._verdict`, so the rule is testable without a process, a socket or
a tmux server, and this module is testable against real ones. Its neighbours:

* :mod:`._verdict_instruments` — the evidence vocabulary (states, reporters,
  SENSORS) and the independence declaration the destruction gate turns on.
* :mod:`._verdict_tmux` — "can we even SEE the host's tmux / pid namespace from
  where we are?" Blindness must come back as "I could not look", never as
  "nothing is there".
* :mod:`._verdict_state` — the signals read from a state ARTEFACT somebody else
  wrote (the heartbeat file, the ``instances`` row). Re-exported below, because
  callers and the suite have always imported them from here.

Every resolver obeys two contracts:

1. **A probe that could not run returns UNKNOWN, never DEAD.** ``False`` and
   "I could not look" are different facts, and only one of them may be acted on.

2. **A resolver declares the INSTRUMENT it actually touched** — the sensor, not
   the reporter. ``process`` and ``registry`` are two reporters, but when the
   runtime is pid-based they are ONE sensor: the same ``os.kill(pid, 0)`` on the
   same pid, guaranteed identical by both runtimes' own docstrings. The
   destruction gate deduplicates on the instrument, so getting this label right
   is what stops one syscall from posing as two witnesses.

Timeouts are deliberately GENEROUS. The host this fleet runs on idles at load
60-70; a 2s probe against it is a coin toss, and a coin-toss timeout that
renders as DEAD is a random agent-killer. Every deadline is sized to be boring
on a loaded box, because the cost of waiting an extra second is nothing and the
cost of a false DEAD is a destroyed agent.
"""

from __future__ import annotations

from typing import Any, Callable

from ._verdict import (
    ALIVE,
    DEAD,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_LISTEN_BROKER,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_PID_NAMESPACE,
    SOURCE_DELIVERY,
    SOURCE_PROCESS,
    UNKNOWN,
    LivenessVerdict,
    Signal,
    decide,
)
from ._verdict_refusal import refusal_signal
from ._verdict_remote import _remote_peer_for_config, remote_process_signal
from ._verdict_screen import screen_signal, started_at_for
from ._verdict_state import HEARTBEAT_STALE_S, heartbeat_signal, registry_signal
from ._verdict_tmux import in_sif as _in_sif  # noqa: F401  (kept as a seam alias)
from ._verdict_tmux import (
    pid_namespace_is_observable,
    session_name_for_config,
    tmux_session_observation,
)
from ._verdict_tmux import tmux_probe_ran as _tmux_probe_ran  # noqa: F401

__all__ = [
    "HEARTBEAT_STALE_S",
    "delivery_signal",
    "heartbeat_signal",
    "process_signal",
    "registry_signal",
    "remote_process_signal",
    "resolve_verdict",
    "screen_signal",
]


# --------------------------------------------------------------------------
# delivery — the ONLY authoritative signal. Never yields DEAD.
# --------------------------------------------------------------------------


def delivery_signal(
    name: str,
    *,
    probe: Callable[[str], tuple[int | None, str]] | None = None,
) -> Signal:
    """Did the broker OBSERVE a live inbox subscriber for ``name``?

    This is the one signal that asks the agent rather than inspecting its
    shadow: the ``sac listen`` broker knows whether the agent's inbox adapter
    is attached, which is the only fact that predicts whether a message will
    actually wake it.

    Deliberately CANNOT return :data:`DEAD` — and that is now MECHANICAL, not a
    convention: :data:`INSTRUMENT_LISTEN_BROKER` is declared to emit only
    ALIVE/UNKNOWN, so a ``DEAD`` here raises at construction. Zero subscribers
    means a detached inbox adapter, not a corpse; an agent with a detached
    adapter is routinely alive and working.

    ``probe`` is the injection seam (a real callable returning the same
    ``(subscribers, reachable)`` tuple); production uses the real
    :func:`.inbox_probe.probe_inbox_reachability`.
    """
    from .._listen._reachability import REACHABLE, UNREACHABLE

    if probe is None:
        from .inbox_probe import probe_inbox_reachability as probe  # type: ignore

    try:
        subscribers, reachable = probe(name)
    except Exception as exc:  # stx-allow: fallback (an unaskable broker is UNKNOWN — never a death verdict against a healthy agent)
        return Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            f"could not ask the listen broker ({type(exc).__name__}) — "
            f"unobserved, NOT unreachable",
            INSTRUMENT_LISTEN_BROKER,
        )

    if reachable == REACHABLE:
        return Signal(
            SOURCE_DELIVERY,
            ALIVE,
            f"{subscribers} live inbox subscriber(s) — the broker can wake it",
            INSTRUMENT_LISTEN_BROKER,
        )
    if reachable == UNREACHABLE:
        # Observed ZERO subscribers on a bus we CAN see. That is evidence the
        # inbox adapter is detached. It is NOT evidence the agent is dead, and
        # rendering it as such would slander (and get us to kill) a healthy,
        # working agent — measured: agents with 0 subscribers and an unbound
        # /v1/turn have answered peer messages the same minute.
        return Signal(
            SOURCE_DELIVERY,
            UNKNOWN,
            "0 inbox subscribers — the inbox adapter is DETACHED, which is "
            "not death; the agent may still be alive and working",
            INSTRUMENT_LISTEN_BROKER,
        )
    return Signal(
        SOURCE_DELIVERY,
        UNKNOWN,
        "the broker cannot observe this agent (no local listen, or it lives "
        "on another host) — unobserved, NOT unreachable",
        INSTRUMENT_LISTEN_BROKER,
    )


# --------------------------------------------------------------------------
# process — a session/pid probe. Ternary, AND instrument-attributed.
# --------------------------------------------------------------------------


def process_signal(
    config: Any,
    runtime: Any,
    *,
    tmux_probe_ran: Callable[[], bool | None] | None = None,
    session_observation: Callable[[str], tuple[bool | None, bool | None]] | None = None,
    in_sif_fn: Callable[[], bool] | None = None,
) -> Signal:
    """Is a process/session for this agent observably up, and WHO SAW IT?

    ``runtime.is_running`` is a BOOL, and its ``False`` conflates two entirely
    different facts — "I probed and there is nothing there" versus "I could not
    probe" — AND, for the TUI runtime, two different INSTRUMENTS. This unpacks
    both.

    The ternary:

    * raises                        → :data:`UNKNOWN` (the probe blew up)
    * ``True``                      → :data:`ALIVE`
    * ``False`` + probe DID run     → :data:`DEAD` (positive: nothing is there)
    * ``False`` + probe did NOT run → :data:`UNKNOWN` (a wedged/invisible tmux)

    THE INSTRUMENT ATTRIBUTION — the part that keeps the destruction gate honest.
    ``TuiSessionRuntime.is_running`` is a CONJUNCTION: ``tmux session exists``
    AND ``os.kill(pane_pid, 0)``. So a ``False`` has two possible authors, and
    only one of them is independent of the registry:

    * the tmux server has no such session → :data:`INSTRUMENT_HOST_TMUX`. A real,
      independent bookkeeper.
    * the session exists but the pane pid is reaped →
      :data:`INSTRUMENT_PID_NAMESPACE`. This is THE SAME ``os.kill(pane_pid, 0)``
      that :func:`._verdict_state.registry_signal` runs, on THE SAME pid
      (``pane_pid_of`` is what feeds ``instances.pid``). Counting it as a second
      witness next to the registry is one syscall wearing two hats — and it is
      how a destruction got authorised on a single reading.

    For a pid-based runtime (apptainer / claude-session) there is no tmux at all:
    ``is_running`` IS ``os.kill(apptainer_pid, 0)``, so it is
    :data:`INSTRUMENT_PID_NAMESPACE` outright — the same instrument the registry
    reads, which means those two can NEVER corroborate each other. That is not a
    regression; it is the truth, finally counted.

    And a pid check from INSIDE a container is not a weak sensor, it is NOT A
    SENSOR (different pid namespace), so it degrades to :data:`UNKNOWN` in BOTH
    directions rather than confidently convicting a healthy host agent.
    """
    runtime_kind = str(getattr(config, "runtime", "") or "")
    is_tui = runtime_kind == "tui"

    try:
        running = runtime.is_running(config)
    except Exception as exc:  # stx-allow: fallback (a probe that raised observed NOTHING — UNKNOWN, never DEAD)
        return Signal(
            SOURCE_PROCESS,
            UNKNOWN,
            f"liveness probe raised {type(exc).__name__}: {exc} — the probe "
            f"did not run, which is not evidence of death",
            INSTRUMENT_NO_OBSERVATION,
        )

    if not is_tui:
        # A pid-based runtime. The ONLY thing is_running consults is
        # os.kill(pid, 0) — the very instrument the registry row check uses.
        observable, why_not = pid_namespace_is_observable(in_sif_fn=in_sif_fn)
        if not observable:
            return Signal(
                SOURCE_PROCESS,
                UNKNOWN,
                f"{runtime_kind or 'runtime'} liveness is a pid check, and {why_not}",
                INSTRUMENT_PID_NAMESPACE,
            )
        if running:
            return Signal(
                SOURCE_PROCESS,
                ALIVE,
                f"{runtime_kind or 'runtime'} probe: the recorded pid is alive",
                INSTRUMENT_PID_NAMESPACE,
            )
        return Signal(
            SOURCE_PROCESS,
            DEAD,
            f"{runtime_kind or 'runtime'} probe succeeded and the recorded pid "
            f"is REAPED — positive evidence of absence. NOTE: this is the same "
            f"os.kill(pid, 0) the registry runs, on the same pid, so the two "
            f"CANNOT corroborate each other",
            INSTRUMENT_PID_NAMESPACE,
        )

    if running:
        return Signal(
            SOURCE_PROCESS,
            ALIVE,
            "tui probe: the tmux session exists and its pane process is alive",
            INSTRUMENT_HOST_TMUX,
        )

    # TUI + not running. WHICH instrument observed the absence?
    if tmux_probe_ran is not None:
        # The legacy seam answers "did a probe run", not "what did it see".
        ran: bool | None = tmux_probe_ran()
        seen: bool | None = None
    else:
        observe = session_observation or (
            lambda s: tmux_session_observation(s, in_sif_fn=in_sif_fn)
        )
        ran, seen = observe(session_name_for_config(config))

    if ran is not True:
        return Signal(
            SOURCE_PROCESS,
            UNKNOWN,
            "the tmux probe itself FAILED (wedged tmux, or this process "
            "cannot see the tmux socket) — 'no session' here means 'I "
            "could not look', not 'the agent is gone'",
            INSTRUMENT_HOST_TMUX,
        )

    if seen is False:
        return Signal(
            SOURCE_PROCESS,
            DEAD,
            "tmux probe SUCCEEDED and the tmux server has NO session for this "
            "agent — positive evidence of absence, from tmux's own bookkeeping",
            INSTRUMENT_HOST_TMUX,
        )

    # The session EXISTS (or, through the legacy seam, we could not tell WHICH
    # half of the conjunction said no). Either way the only thing that can have
    # failed is the pane-pid check — os.kill(pane_pid, 0) — which is the
    # registry's instrument, not an independent one. THE AMBIGUITY RULE: label
    # the instrument that COLLAPSES, never the one that invents a witness.
    return Signal(
        SOURCE_PROCESS,
        DEAD,
        "tmux probe SUCCEEDED; the session is present but its pane process is "
        "gone — os.kill(pane_pid, 0) says REAPED. That is the SAME syscall on "
        "the SAME pid the registry checks, so it is NOT independent of it",
        INSTRUMENT_PID_NAMESPACE,
    )


# --------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------


def resolve_verdict(
    name: str,
    config: Any | None = None,
    runtime: Any | None = None,
    *,
    delivery: Callable[[str], Signal] | None = None,
    process: Callable[[Any, Any], Signal] | None = None,
    heartbeat: Callable[[str], Signal] | None = None,
    registry: Callable[[str], Signal] | None = None,
    screen: Callable[[str], Signal] | None = None,
    refusal: Callable[[str, Any], Signal] | None = None,
) -> LivenessVerdict:
    """Gather every signal we can, then fold them with :func:`._verdict.decide`.

    Signals we cannot gather are simply absent — an absent signal contributes
    nothing, which is right: it neither convicts nor exonerates. A verdict with
    no signals at all is :data:`UNKNOWN`, and that is the honest answer.

    Every collaborator is an injection seam taking REAL callables (no mocks —
    the suite drives real tmux sockets, real processes, real files through
    these). ``screen`` (the WORKING sensor) folds in for a TUI agent only — see
    :func:`._verdict_screen.screen_signal`.
    """
    signals: list[Signal] = []
    kind = str(getattr(config, "runtime", "") or "") if config is not None else ""

    signals.append((delivery or delivery_signal)(name))

    if config is not None and runtime is not None:
        peer = _remote_peer_for_config(config)
        if peer is not None and process is None:
            # Remote agent: the LOCAL tmux is blind to it (its session lives on
            # the peer), so the ordinary process_signal reads DEAD and the
            # agent vanishes from `sac agents list`. Probe the peer's tmux over
            # ssh instead — control-plane cross-host liveness. An injected
            # `process` still wins, so the local verdict suite is unaffected.
            signals.append(remote_process_signal(config, peer))
        else:
            signals.append((process or process_signal)(config, runtime))

    if heartbeat is None:
        # runtime_kind decides WHOSE writer produced the beat, hence which
        # instrument it is. For a TUI agent it is a re-report of the same tmux
        # snapshot process_signal reads — not a second sensor.
        signals.append(heartbeat_signal(name, runtime_kind=kind))
    else:
        signals.append(heartbeat(name))

    signals.append((registry or registry_signal)(name))

    # SCREEN — the WORKING sensor. A wedged TUI agent's tmux session exists and
    # its pane pid is alive, so process/heartbeat/registry all read PRESENCE-ALIVE
    # while it does nothing. The screen signal reads the pane's rendered CONTENT
    # (a frozen auth banner) and reports WEDGED, which decide() ranks ABOVE those
    # presence-ALIVEs. TUI only — a pid-based runtime has no tui pane to read. An
    # injected ``screen`` always wins (the test seam); otherwise we read the auth
    # cache, passing this incarnation's started_at so a pre-restart (SUPERSEDED)
    # verdict is discarded rather than read as wedged.
    if screen is not None:
        signals.append(screen(name))
    elif kind == "tui":
        signals.append(screen_signal(name, started_at=started_at_for(name)))

    # REFUSAL — the CAN-IT-ACT sensor. Every signal above answers "is it
    # PRESENT?"; a quota-dead or credential-dead agent scores perfectly on all
    # of them and cannot execute a single turn (scitex-cards, 2026-08-10: 32
    # minutes of "You've hit your weekly limit" behind a HEALTHY row). This
    # reads the agent's OWN transcript for a refused last turn. Runtime-agnostic
    # — the SDK runtimes hit the same provider walls — and never raises, so a
    # health command can never be taken down by an unreadable file.
    if refusal is not None:
        signals.append(refusal(name, config))
    elif config is not None:
        signals.append(refusal_signal(name, config=config))

    return decide(name, signals)
