"""Asking systemd what a unit ACTUALLY runs, and parsing what it says.

The read half of the audit. Every failure mode here becomes an explicit
UNKNOWN carrier (``UnitState.error``), never a silent empty result — and
stderr is CAPTURED AND CARRIED, never discarded. Discarding stderr
discards the only channel that reports the failure you did not
anticipate; that exact pattern (``2>/dev/null``) hid a dead cron job on
this host for 49 days.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess

from ._model import UnitState

#: How long to let a single ``systemctl show`` call take. It is a local
#: read against the user bus; anything slower is a sick bus, which is an
#: UNKNOWN worth surfacing rather than a hang worth waiting on.
QUERY_TIMEOUT_SEC = 10


def unit_name_for(job) -> str:
    """The unit scitex-dev derives for ``job``.

    The job name is taken VERBATIM — ``sac.accounts-refresh`` becomes
    ``sac.accounts-refresh.service``. That verbatim derivation is exactly
    why ``sac listen`` must never be federated (a ``sac.listen`` JobSpec
    would materialise ``sac.listen.service``, which does NOT adopt the
    hand-written ``sac-listen.service``); see ``_jobs_plugin.provide_jobs``.
    """
    return f"{job.name}.service"


def _argv_from_execstart(value: str) -> str | None:
    """Pull the ``argv[]=...`` field out of one ExecStart record."""
    marker = "argv[]="
    start = value.find(marker)
    if start == -1:
        return None
    rest = value[start + len(marker) :]
    # Fields are ``;``-separated inside the ``{ ... }`` record.
    end = rest.find(" ; ")
    argv = rest[:end] if end != -1 else rest.rstrip(" }")
    return argv.strip() or None


def parse_show_output(text: str) -> UnitState:
    """Parse ``systemctl show -p LoadState -p ExecStart`` key=value output.

    ``show`` exits 0 even for a unit that does not exist, printing only
    ``LoadState=not-found`` and omitting ``ExecStart`` entirely — so the
    EXIT CODE IS USELESS as a discriminator and ``LoadState`` is the crisp
    one. (Measured against systemd 249 on the fleet host: a nonexistent
    unit gives rc=0, empty stdout for ``--value``, and empty stderr.)

    The ``ExecStart`` value is a structured record::

        { path=/x/bin/sac ; argv[]=/x/bin/sac accounts refresh --all ; ... }

    ``argv[]`` is the actual exec vector and the only part worth
    comparing; ``path``, ``pid``, ``status`` and the timestamps are
    runtime noise that differs between two identical runs.
    """
    load_state: str | None = None
    execstart: str | None = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "LoadState":
            load_state = value.strip()
        elif key == "ExecStart" and value.strip():
            execstart = _argv_from_execstart(value)
    return UnitState(load_state=load_state, execstart=execstart)


def query_unit(unit: str, *, runner=subprocess.run, which=shutil.which) -> UnitState:
    """Ask systemd what ``unit`` actually runs.

    Failure modes, each an explicit UNKNOWN carrier:

    * ``systemctl`` not on PATH — the container case, and the common one:
      this package's own agents run in an Apptainer image with no systemd.
    * a non-zero exit / no user bus — a session without a user manager.
    * a timeout — a wedged bus.
    * rc=0 but an unparseable shape — reported verbatim rather than
      guessed at.

    ``runner`` and ``which`` are test seams (fake-callables), the same
    convention scitex-dev's own ``resolve_execstart`` uses for exactly
    this reason. They let the suite point at a REAL fixture ``systemctl``
    script on disk — a real program, not a mock — so the parsing and the
    verdicts are exercised against genuine subprocess behaviour on a
    machine that may have no systemd at all.
    """
    exe = which("systemctl")
    if exe is None:
        return UnitState(
            load_state=None,
            execstart=None,
            error=(
                "`systemctl` is not on PATH — there is no systemd to ask "
                "(the normal case inside a container; run this on the host)"
            ),
        )
    try:
        proc = runner(
            [exe, "--user", "show", "-p", "LoadState", "-p", "ExecStart", unit],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return UnitState(
            load_state=None,
            execstart=None,
            error=(
                f"`systemctl --user show {unit}` did not return within "
                f"{QUERY_TIMEOUT_SEC}s — the user bus is not answering"
            ),
        )
    except OSError as exc:
        return UnitState(
            load_state=None,
            execstart=None,
            error=f"could not exec systemctl: {exc}",
        )

    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return UnitState(
            load_state=None,
            execstart=None,
            error=(
                f"`systemctl --user show {unit}` exited {proc.returncode}"
                + (f": {stderr}" if stderr else " with no stderr")
            ),
        )

    state = parse_show_output(proc.stdout or "")
    if state.load_state is None:
        # rc=0 but no LoadState at all: not a shape we know how to read.
        # Report it rather than deriving a verdict from it.
        return UnitState(
            load_state=None,
            execstart=None,
            error=(
                f"`systemctl --user show {unit}` returned no LoadState; "
                f"stdout={proc.stdout!r}" + (f" stderr={stderr!r}" if stderr else "")
            ),
        )
    # A rc=0 call that STILL wrote to stderr is worth carrying forward.
    return UnitState(
        load_state=state.load_state,
        execstart=state.execstart,
        error=stderr or None,
    )


def commands_equal(intended: str, resolved: str) -> bool:
    """Compare two ExecStart command strings by their token vectors.

    ``shlex`` both sides so quoting differences that do not change the
    exec vector are not reported as a divergence. Only a real difference
    in what gets executed counts.
    """
    try:
        return shlex.split(intended) == shlex.split(resolved)
    except ValueError:
        # Unbalanced quotes on either side — cannot tokenize, so fall back
        # to an exact comparison rather than claiming equality.
        return intended == resolved


__all__ = [
    "QUERY_TIMEOUT_SEC",
    "commands_equal",
    "parse_show_output",
    "query_unit",
    "unit_name_for",
]
