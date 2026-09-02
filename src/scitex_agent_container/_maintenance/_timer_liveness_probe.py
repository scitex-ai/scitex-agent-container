"""The reading half of the timer-liveness check: enumerate, ask, fold.

Split from :mod:`._timer_liveness_model` on the convention this package
already follows (``_worktree_gc_model`` / ``_worktree_gc_probe``): everything
there is pure and asserts the RULE; everything here touches the host and
asserts the READING. The classification itself never runs a subprocess, which
is what lets a recorded capture be classified by exactly the code that
classifies a live host.

A DETECTOR, and nothing more. It runs ``systemctl show`` and returns a
verdict. It never starts, restarts, re-arms, enables, disables or removes a
unit, and it changes nothing about how sac's jobs are scheduled. The repair —
including the case where the correct repair is REMOVAL rather than revival —
is prose in :meth:`._timer_liveness_model.TimerLivenessVerdict.hint`, for a
human to carry out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ._timer_liveness_model import (
    ARMING_OK,
    ARMING_UNKNOWN,
    ARMING_VIOLATION,
    SHOW_PROPERTIES,
    TIMER_NEVER_AGAIN,
    TIMER_UNKNOWN,
    TimerLivenessVerdict,
    TimerReading,
)

__all__ = [
    "check_timer_liveness",
    "enabled_timer_units",
    "read_capture_dir",
    "verdict_for",
]


def verdict_for(
    readings: "tuple[TimerReading, ...] | list[TimerReading]",
    *,
    scanned: bool = True,
    source: str = "systemctl --user",
    detail: str = "",
) -> TimerLivenessVerdict:
    """Fold per-unit classifications into one host verdict.

    Pure over its input — the seam that lets the whole rule be asserted
    against recorded captures with no systemd anywhere near the test.

    A dead timer OUTRANKS an unreadable one: VIOLATION is what a human must
    act on, and demoting it to UNKNOWN because some OTHER unit was unreadable
    would hide the finding behind the blindness. An unreadable unit with no
    dead one still refuses to say OK, for the reason in the model's docstring.
    """
    ordered = tuple(readings)
    if not scanned:
        return TimerLivenessVerdict(
            state=ARMING_UNKNOWN,
            readings=ordered,
            scanned=False,
            source=source,
            detail=detail,
        )
    if any(r.state == TIMER_NEVER_AGAIN for r in ordered):
        state = ARMING_VIOLATION
    elif any(r.state == TIMER_UNKNOWN for r in ordered):
        state = ARMING_UNKNOWN
    else:
        state = ARMING_OK
    return TimerLivenessVerdict(
        state=state, readings=ordered, scanned=True, source=source, detail=detail
    )


def read_capture_dir(capture_dir: "str | Path") -> tuple[TimerReading, ...]:
    """Read recorded ``systemctl show`` captures out of a directory.

    One ``*.txt`` per unit, named after the unit. This is a real vantage, not
    a seam bolted on for tests: the incident's evidence arrived as pasted
    ``systemctl show`` output from a host, and a capture taken on a machine
    sac cannot run on is classifiable afterwards by exactly the code that
    classifies a live host.

    A file that cannot be read at all becomes an UNKNOWN reading rather than
    an exception — one unreadable capture must not destroy the verdict on the
    rest of the directory.
    """
    root = Path(capture_dir)
    readings: list[TimerReading] = []
    for path in sorted(root.glob("*.txt")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            readings.append(
                TimerReading(unit=path.stem, read_error=f"capture unreadable: {exc}")
            )
            continue
        readings.append(TimerReading.from_show_output(path.stem, text))
    return tuple(readings)


def _run(argv: list[str], timeout: int) -> tuple[str, str]:
    """``(stdout, error)`` — never raises, so one bad unit cannot end the pass.

    A non-zero exit with usable stdout is NOT an error: ``systemctl show`` is
    routinely non-zero for a unit it still describes, and discarding those
    rows would turn readable timers into unknowns.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0 and not proc.stdout.strip():
        detail = proc.stderr.strip().splitlines()
        return "", detail[0] if detail else f"exit {proc.returncode}"
    return proc.stdout, ""


def enabled_timer_units(timeout: int = 15) -> tuple[tuple[str, ...], str]:
    """``(unit names, error)`` for every ENABLED ``--user`` timer.

    Enabled rather than loaded: a timer nobody enabled is not a promise
    anybody relies on, and the fault this module exists for is a unit the host
    still promises to run while it cannot.
    """
    stdout, error = _run(
        [
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=timer",
            "--state=enabled",
            "--no-legend",
        ],
        timeout,
    )
    if error:
        return (), error
    units = tuple(line.split()[0] for line in stdout.splitlines() if line.split())
    return units, ""


def check_timer_liveness(
    *,
    capture_dir: "str | Path | None" = None,
    timeout: int = 15,
) -> TimerLivenessVerdict:
    """The verdict for this host — or for a directory of recorded captures.

    ``capture_dir`` classifies captures already on disk and runs no subprocess
    at all. Without it the check enumerates enabled ``--user`` timers and
    reads each one's next elapse.

    An enumeration that FAILS yields ``scanned=False`` and UNKNOWN: zero
    timers found because nobody could look is not zero timers.
    """
    if capture_dir is not None:
        return verdict_for(
            read_capture_dir(capture_dir), source=f"captures: {capture_dir}"
        )

    units, error = enabled_timer_units(timeout)
    if error:
        return verdict_for((), scanned=False, detail=error)

    readings: list[TimerReading] = []
    for unit in units:
        argv = ["systemctl", "--user", "show", unit]
        for prop in SHOW_PROPERTIES:
            argv += ["-p", prop]
        stdout, show_error = _run(argv, timeout)
        if show_error:
            readings.append(
                TimerReading(
                    unit=unit, read_error=f"systemctl show failed: {show_error}"
                )
            )
            continue
        readings.append(TimerReading.from_show_output(unit, stdout))
    return verdict_for(readings)
