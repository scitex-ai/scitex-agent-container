"""Run ``sac agents restart -y <agent>`` and bring back EVERY byte it produced.

WHY THIS EXACT INVOCATION
    ``sac agents restart -y <name>`` is the operator's own alias, the one he has
    verified by hand many times. An auto-restarter whose restart differs from
    the one a human has confirmed works is a second thing to debug at the worst
    possible moment, so nothing here is invented: no ``--force`` (``restart``
    has no such flag), no ``--json`` (that envelope exists for cross-host
    dispatch; we want the human-readable diagnostics in the log), no substitute
    in-process call path.

    The one thing that IS resolved is WHICH ``sac`` binary — a host can carry
    several installs at several versions, and the log must name the one that
    actually ran (see the invocation-form-selects-the-binary class). If none can
    be found, that is a REPORTED failure, never a quiet substitution.

WHY THE OUTPUT IS A FIELD AND NOT A PRINT
    The deployed ``auth-heal.py`` called ``subprocess.run(capture_output=True)``
    and then read only ``returncode``. Every restart it ever performed had its
    stdout and stderr collected and dropped on the floor at the moment of
    collection, so when those restarts silently stopped working there was
    nothing to read. Carrying both WHOLE in :class:`RestartResult` is what makes
    discarding them the awkward path rather than the default one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

__all__ = [
    "RESTART_TIMEOUT_S",
    "RestartResult",
    "restart_command",
    "run_sac_restart",
]

#: Explicit override for the ``sac`` executable, so a test can point the pass at
#: a REAL script instead of the installed CLI (no mocks) and an operator can pin
#: a specific install on a host carrying several.
_SAC_BIN_ENV = "SAC_BIN"

#: How long one ``sac agents restart`` may take before we stop waiting. A
#: restart that has not returned by now has its own problem; the timeout is
#: reported as a failure carrying its partial output, never as a silent success.
RESTART_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class RestartResult:
    """Everything one ``sac agents restart -y <name>`` produced.

    ``returncode`` is ``None`` when the command never got to exit at all — it
    could not be launched, or it timed out. That is deliberately NOT folded into
    a non-zero code: "it ran and failed" and "we do not know whether it ran" are
    different facts, and only the first is a clean failure.
    """

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def restart_command(name: str) -> list[str]:
    """The EXACT operator-verified invocation for ``name``, with ``sac`` resolved.

    Raises ``FileNotFoundError`` when no ``sac`` can be found, rather than
    falling back to any other way of restarting an agent. A silent fallback to a
    different mechanism is how a fleet ends up with two restart paths and one
    set of expectations.
    """
    binary = os.environ.get(_SAC_BIN_ENV) or shutil.which("sac")
    if not binary:
        raise FileNotFoundError(
            "no `sac` executable on PATH (and $SAC_BIN is unset), so the "
            "operator-verified restart command cannot be run. REFUSING to "
            "substitute a different invocation"
        )
    return [binary, "agents", "restart", "-y", name]


def run_sac_restart(name: str) -> RestartResult:
    """Run the restart as a subprocess. Never raises — every outcome is a RESULT.

    A failure to launch, a non-zero exit and a timeout all come back as
    :class:`RestartResult` carrying whatever output was produced before they
    happened. A raise here would lose exactly the diagnostics the caller exists
    to write down.
    """
    started = time.monotonic()
    # stx-allow: fallback (reason: an unresolvable `sac` is a REPORTABLE result
    # carrying its reason, not a crash that would abort the sweep over the rest
    # of the wedged fleet)
    try:
        argv = restart_command(name)
    except FileNotFoundError as exc:
        return RestartResult(
            argv=("sac", "agents", "restart", "-y", name),
            returncode=None,
            stdout="",
            stderr="",
            duration_s=0.0,
            error=str(exc),
        )
    # stx-allow: fallback (reason: each branch is a distinct RESULT that must
    # reach the log with its partial output intact — a timeout in particular
    # must never read as either a success or a clean failure)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=RESTART_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return RestartResult(
            argv=tuple(argv),
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            duration_s=time.monotonic() - started,
            error=(
                f"timed out after {RESTART_TIMEOUT_S:.0f}s — the restart did not "
                f"return, so whether it took effect is UNKNOWN"
            ),
        )
    except OSError as exc:
        return RestartResult(
            argv=tuple(argv),
            returncode=None,
            stdout="",
            stderr="",
            duration_s=time.monotonic() - started,
            error=f"could not execute {argv[0]}: {exc}",
        )
    return RestartResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration_s=time.monotonic() - started,
    )


def _text(raw: "str | bytes | None") -> str:
    """``TimeoutExpired`` hands back bytes even in text mode. Keep the payload."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
