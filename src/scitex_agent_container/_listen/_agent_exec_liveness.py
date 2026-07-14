"""Post-ack liveness probe for brokered spawns (extracted from _agent_exec).

Layer-3 fail-loud (clew dogfood repro 2026-06-06, lead msg 57f1632a): after
``sac agents start <child>`` returns rc=0, the listen waits a grace window for
the real apptainer instance to come up before accepting the spawn as healthy —
a SIF that comes up and dies silently must not be reported as SUCC.

Split out of :mod:`scitex_agent_container._listen._agent_exec` to keep that
module under the per-file line cap.

THE PROBE USED TO CONVICT EVERY TUI AGENT (fixed here)
------------------------------------------------------
This probe waits for ``<runtime_dir>/apptainer_pid`` to appear. Only
:class:`runtimes._apptainer_runtime.ApptainerContainerRuntime` ever WRITES that
file — a TUI agent launches through tmux and **never writes one, by
construction** (``cli_pkg/_helpers/_agent_list`` says so in as many words).

``tui`` is this fleet's DEFAULT runtime. So every TUI agent brokered through
``POST /agents`` waited out the grace window, failed to find a pidfile it was
never going to write, and got stamped ``startup_failed`` /
``post_ack_no_apptainer_pid`` + a 502 — while being perfectly alive. Measured on
the live fleet 2026-07-14: ``grant`` carried exactly that marker while holding a
live tmux session, a fresh heartbeat and **1 live inbox subscriber**
(``inbox_reachable: reachable``); ``scitex-writer`` carried it while ANSWERING a
peer's message. That bogus ``startup_failed`` is then read downstream as "this
agent is dead", whose remedy is ``--force --fresh`` — so a probe looking for the
wrong file talked operators into destroying healthy agents.

The fix: ask the agent's OWN runtime whether it is up, instead of looking for an
artefact only one runtime produces. And — the load-bearing half — an
INCONCLUSIVE probe now returns "no failure" rather than manufacturing one:
UNKNOWN authorises nothing, least of all a ``startup_failed`` stamp.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "_POST_ACK_LIVENESS_TIMEOUT_S",
    "_POST_ACK_LIVENESS_POLL_INTERVAL_S",
    "_pid_alive",
    "_probe_post_ack_liveness",
    "_runtime_writes_apptainer_pidfile",
]

# Grace window the listen waits AFTER `sac agents start <child>` returns rc=0
# before it accepts the spawn as "running and healthy".
#
# 20s, not the original 5s. The host this fleet runs on idles at load 60-70;
# under that load an apptainer instance routinely needs more than five seconds
# to write its pidfile, so a 5s deadline was not a probe, it was a coin toss —
# and a lost toss stamps ``startup_failed`` on a healthy agent, whose remedy is
# destructive. A generous deadline costs a few seconds on a genuine failure and
# buys us not killing working agents. That trade is not close.
_POST_ACK_LIVENESS_TIMEOUT_S = 20.0

# How often to re-check the apptainer_pid file inside the grace window.
_POST_ACK_LIVENESS_POLL_INTERVAL_S = 0.1


def _runtime_writes_apptainer_pidfile(name: str) -> bool | None:
    """Does ``name``'s declared runtime write ``<state_dir>/apptainer_pid``?

    ``True``  — an apptainer/SDK-style runtime; the pidfile probe is valid.
    ``False`` — a ``tui`` runtime; it launches via tmux and NEVER writes one, so
                waiting for it is guaranteed to "fail" and means nothing.
    ``None``  — we could not resolve the spec. UNKNOWN, and the caller must
                then not convict.
    """
    try:
        from ..config import load_config, resolve_config

        config = load_config(resolve_config(name))
    except Exception:  # stx-allow: fallback (an unresolvable spec is UNKNOWN — the caller must not manufacture a failure from it)
        return None
    runtime_kind = str(getattr(config, "runtime", "") or "").strip().lower()
    if not runtime_kind:
        return None
    return runtime_kind != "tui"


def _probe_runtime_is_up(name: str) -> bool | None:
    """Ask the agent's OWN runtime whether it is up. Ternary.

    ``True`` alive / ``False`` observably absent / ``None`` the probe could not
    run (a wedged tmux, an unresolvable spec) — which is UNKNOWN and must never
    be rendered as a startup failure.
    """
    try:
        from .._lifecycle._runtime_select import _get_runtime
        from .._lifecycle._verdict import ALIVE, DEAD
        from .._lifecycle._verdict_resolve import process_signal
        from ..config import load_config, resolve_config

        config = load_config(resolve_config(name))
        signal = process_signal(config, _get_runtime(config))
    except Exception:  # stx-allow: fallback (a probe that blew up observed NOTHING — UNKNOWN, never a failure verdict)
        return None
    if signal.verdict == ALIVE:
        return True
    if signal.verdict == DEAD:
        return False
    return None


def _pid_alive(pid: int) -> bool:
    """``kill -0`` style liveness check. Returns False iff pid is reaped.

    ``PermissionError`` means the kernel refused us (e.g. pid owned by
    another uid), but the process exists — we treat that as alive.
    Only ``ProcessLookupError`` (and the equivalent ``OSError(ESRCH)``)
    counts as dead. ``pid <= 0`` is treated as dead defensively (the
    file may have been written truncated / partial).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _probe_post_ack_liveness(
    runtime_dir: Path,
    *,
    name: str | None = None,
    timeout_s: float = _POST_ACK_LIVENESS_TIMEOUT_S,
    poll_interval_s: float = _POST_ACK_LIVENESS_POLL_INTERVAL_S,
    writes_pidfile_fn: Callable[[str], bool | None] | None = None,
    runtime_is_up_fn: Callable[[str], bool | None] | None = None,
) -> tuple[str, str] | None:
    """Return ``None`` if the spawned instance is live by deadline.

    Returns a ``(kind, hint)`` tuple ONLY on a POSITIVELY OBSERVED failure,
    suitable for the :func:`_lifecycle._startup_failed.write_marker`
    ``kind_override`` + the operator-facing diagnostic hint:

    * ``"post_ack_no_apptainer_pid"`` — an apptainer-runtime child returned 0
      but never wrote ``<runtime_dir>/apptainer_pid``. The wrapper bypassed the
      apptainer runtime path entirely (broken config, missing image, runtime
      mis-resolved, etc.).
    * ``"post_ack_apptainer_pid_dead"`` — ``apptainer_pid`` was written but the
      process is dead (or never was alive). The SIF instance came up and
      immediately exited; ``stderr.log`` in the runtime dir usually has the
      actual cause.
    * ``"post_ack_session_absent"`` — a NON-apptainer (e.g. ``tui``) agent whose
      own runtime reports, from a probe that SUCCEEDED, that no session/process
      is there.

    ``name`` selects WHICH probe is valid for this agent, and passing it is what
    stops the pidfile probe convicting every TUI agent in the fleet (see the
    module docstring — TUI agents never write ``apptainer_pid``, so the old
    unconditional pidfile wait stamped ``startup_failed`` on healthy agents).
    ``name=None`` keeps the legacy pidfile-only behaviour for callers that have
    no agent name to resolve.

    UNKNOWN NEVER PRODUCES A FAILURE. If we cannot resolve the spec, or the
    runtime probe itself could not run, we return ``None`` (no failure). A probe
    that could not run is not evidence of a failed start, and rendering it as one
    hands the caller a death verdict whose remedy destroys a healthy agent.
    """
    if timeout_s <= 0.0:
        # Test escape hatch: zero/negative timeout skips the probe. The
        # listen handler honours SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S
        # to set this from the env so legacy tests that don't simulate
        # the apptainer-pid write can opt out (the probe's contract is
        # asserted by the dedicated probe-tests below).
        return None

    if name:
        writes = (writes_pidfile_fn or _runtime_writes_apptainer_pidfile)(name)
        if writes is None:
            # We could not tell which runtime this is. UNKNOWN — say nothing.
            return None
        if not writes:
            return _probe_non_apptainer_runtime(
                name,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                runtime_is_up_fn=runtime_is_up_fn or _probe_runtime_is_up,
            )

    pid_file = runtime_dir / "apptainer_pid"
    deadline = time.monotonic() + max(0.0, timeout_s)
    saw_pid_file = False
    last_seen_pid: int | None = None

    while True:
        if pid_file.is_file():
            saw_pid_file = True
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = -1
            last_seen_pid = pid
            if pid > 0 and _pid_alive(pid):
                return None  # live → happy path
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)

    if not saw_pid_file:
        return (
            "post_ack_no_apptainer_pid",
            f"`sac agents start` returned rc=0 but no apptainer_pid "
            f"file appeared in {runtime_dir} within "
            f"{timeout_s:.1f}s. The child subprocess bypassed the "
            "apptainer runtime path (broken wrapper / missing config / "
            "runtime mis-resolved). Inspect the runtime_dir for any "
            "stdout/stderr the child captured, and verify the agent's "
            "spec.apptainer block resolves to a real SIF on disk.",
        )
    return (
        "post_ack_apptainer_pid_dead",
        f"apptainer_pid={last_seen_pid} in {runtime_dir} was reaped "
        f"within {timeout_s:.1f}s of the spawn-ack. The SIF instance "
        "came up and exited silently — check the runtime_dir's "
        "stderr.log for the apptainer FATAL line. Common causes: "
        "missing bind source on host, SIF binary missing on host, "
        "in-SIF entrypoint crashing on a startup-prompt eval.",
    )


def _probe_non_apptainer_runtime(
    name: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
    runtime_is_up_fn: Callable[[str], bool | None],
) -> tuple[str, str] | None:
    """Post-ack probe for a runtime that writes NO ``apptainer_pid`` (i.e. tui).

    Asks the agent's own runtime whether its session/process is up, polling
    until the deadline so a slow boot on a loaded host is not mistaken for a
    failure.

    Returns ``None`` (no failure) for BOTH "alive" and "could not tell". Only a
    probe that SUCCEEDED and positively observed absence produces a failure —
    which for a TUI agent means: the tmux probe ran, and there is no live
    ``tui-<name>`` session or pane process.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    last: bool | None = None
    while True:
        last = runtime_is_up_fn(name)
        if last is True:
            return None  # observably alive → happy path
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval_s)

    if last is None:
        # The probe never actually ran (wedged tmux, unresolvable spec, a
        # prober that cannot see the tmux socket). UNKNOWN authorises nothing:
        # emphatically not a `startup_failed` stamp on an agent that may be
        # perfectly alive.
        return None

    return (
        "post_ack_session_absent",
        f"`sac agents start` returned rc=0 for {name!r}, but {timeout_s:.1f}s "
        f"later its runtime still reports no live session/process — and the "
        f"probe SUCCEEDED, so this is a real absence, not a failed look. The "
        f"agent booted and went away. Check the runtime dir's boot.stderr.log "
        f"/ start_failure_diag.log for the cause.",
    )
