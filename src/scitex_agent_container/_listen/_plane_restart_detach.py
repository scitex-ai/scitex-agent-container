"""Run a plane-restarting command DETACHED, so it cannot kill its own reporter.

Paired with :mod:`._plane_targeting_argv`, which decides WHETHER a host_exec
command would restart the listen daemon serving the request. This module is the
other half: HOW to run it so the caller still gets an answer.

Kept separate from the predicate on purpose — that one is pure and unit-testable
with no I/O, this one spawns processes. Kept out of ``_host_exec`` because that
file is near its line cap and this is a distinct responsibility.

THE MECHANISM IS NOT NEW. ``_listen/_agent_restart.py`` already solved the
identical hazard for agent self-restart (incident 2026-07-12): a synchronous
bounce deadlocks because the caller cannot die while still awaiting the response
it is blocked on, so the restart is handed to a ``setsid`` child that sleeps
past the response flush, and the handler answers 202 immediately. The same three
ingredients are reproduced here — detach, delay, log — deliberately, so the
fleet has ONE answer to "don't decapitate yourself" rather than three that drift.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

__all__ = [
    "PLANE_RESTART_DELAY_S",
    "build_detached_command",
    "plane_restart_log_path",
    "spawn_detached_plane_command",
]

# Delay before the detached command actually fires. Long enough for the 202 to
# flush over the wire and the caller's tool-call to unwind BEFORE the daemon
# goes down — the whole failure being that a caller cannot receive a response
# from a process that is being killed. Mirrors
# ``_agent_restart._SELF_RESTART_DELAY_S`` (3s) deliberately: same hazard, same
# number, so the two cannot drift into different answers.
PLANE_RESTART_DELAY_S = 3


def plane_restart_log_path() -> str:
    """Where a detached plane command writes its output.

    NEVER ``/dev/null``. The command necessarily outlives the process that could
    have reported it, so this log is the only post-hoc evidence of what it did —
    which is precisely what was missing when the inline form returned
    ``exit_code -15`` with an empty stdout.
    """
    return str(
        Path.home()
        / ".scitex"
        / "agent-container"
        / "runtime"
        / "logs"
        / "host_exec-plane-restart.log"
    )


def build_detached_command(
    argv: list[str] | tuple[str, ...],
    *,
    delay_s: int,
    log_path: str,
) -> list[str]:
    """Build the ``setsid sh -c '<inner>'`` argv (PURE — spawns nothing).

    Separated from the spawn so the constructed command is unit-assertable
    without forking anything, the same seam ``_agent_restart`` uses. ``<inner>``
    is::

        sleep <delay_s>; ( echo <marker>; date -Is; <cmd> ) >> <log> 2>&1

    Every token is ``shlex.quote``d — the caller's argv is a list by contract
    (``host_exec`` rejects the shell form), and it must stay one command rather
    than becoming shell text that could word-split.
    """
    inner = " ".join(shlex.quote(tok) for tok in argv)
    marker = shlex.quote(f"=== host_exec detached plane command: {inner} ===")
    script = (
        f"sleep {int(delay_s)}; "
        f"( echo {marker}; date -Is; {inner} ) >> {shlex.quote(log_path)} 2>&1"
    )
    return ["setsid", "sh", "-c", script]


def spawn_detached_plane_command(
    argv: list[str] | tuple[str, ...],
    *,
    env: dict[str, str] | None,
    log_path: str,
    delay_s: int = PLANE_RESTART_DELAY_S,
) -> list[str]:
    """Fire-and-forget ``argv`` after ``delay_s``, fully detached. Returns the argv.

    ``setsid`` plus ``start_new_session=True`` (belt and braces) sever the child
    from this handler's session AND process group, so restarting the daemon does
    not take the child with it — which is exactly what happens inline. stdin is
    ``/dev/null``; stdout/stderr go to ``log_path`` via the inner shell, so the
    child holds no pipe to a process that is about to die.

    Raises ``OSError`` if the spawn itself fails; the caller must surface that
    rather than reporting a fake "scheduled".
    """
    detached = build_detached_command(argv, delay_s=delay_s, log_path=log_path)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        detached,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return detached
