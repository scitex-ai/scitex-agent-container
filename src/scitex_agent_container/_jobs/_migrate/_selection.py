"""The run-selection knob: WHICH declared jobs actually run on this host.

WHY THIS IS NEEDED AT ALL
=========================
A JobSpec is a fleet-wide declaration with no host axis — measured on the
real model, ``JobSpec`` has ``name/kind/schedule/command/description/
on_boot_sec/on_unit_active_sec/timeout_sec/restart_policy/watchdog_sec/
venv`` and nothing else. So every discovered job is a candidate on every
host, and "which of these should run HERE" has, until now, been answered
only by the host's systemd enablement — invisible state, on nine hosts,
with no declaration to compare it against.

sac's own specs are full of jobs that must NOT all run together:
``restart-login-expired-agents`` and ``heal-agent-auth`` are explicitly
mutually exclusive (two restarters with independent debounce state), and
``accounts-keepalive`` must run ONLY on the host holding refresh material.
Today those constraints live in prose inside docstrings. This makes the
answer a file.

THE THIRD STATE IS THE WHOLE DESIGN
===================================
:func:`selection` returns ``None`` for UNSTATED, which is different from
"nothing selected". A host that has never been configured must not have
its timers silently disarmed by the arrival of this feature; a host that
deliberately selected nothing must not have them armed. Collapsing those
two into an empty set is how a safety feature becomes an outage — the
same three-state discipline ``_dev_jobs_backend.resolve`` uses for
"cannot tell".

IT GATES ARMING, NOT INSTALLING
===============================
Selection decides what is ENABLED (armed to fire), never what is
installed. Install writes an inert unit file; that is cheap, reversible,
and makes the unit inspectable with ``systemctl cat``. Refusing to install
would instead hide the job from the very command an operator uses to ask
what this host could run.
"""

from __future__ import annotations

from pathlib import Path

from .. import _names

#: Env override for which jobs run here. Comma- or newline-separated LOCAL
#: names. Primarily the test seam, and the way a one-off host states its
#: selection without committing a file.
SELECTION_ENV = "SAC_JOBS_ENABLED"

#: Per-host selection file, relative to ``$HOME``. Configuration, so it is
#: a FILE under the operator's control (Wave A1: "states -> PostgreSQL,
#: configuration -> files"). One local name per line, ``#`` comments,
#: blank lines ignored.
SELECTION_FILE = ".scitex/agent-container/jobs-enabled.txt"

#: The token selecting every declared job. Spelled out rather than implied
#: by an empty file, because an empty file and an absent one MUST NOT mean
#: opposite things — and without this token "run everything" would have no
#: way to be written down at all.
SELECT_ALL = "*"


def parse_selection(text: str) -> frozenset[str]:
    """Parse a selection body into local job names.

    Commas are accepted as separators alongside newlines so the env form
    and the file form parse identically — one grammar, not two.
    """
    out: set[str] = set()
    for raw in text.replace(",", "\n").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return frozenset(out)


def selection_path(home: Path) -> Path:
    """Where this host's selection file lives."""
    return home / SELECTION_FILE


def selection(*, env: dict[str, str], home: Path) -> frozenset[str] | None:
    """Which jobs are selected to run here, or ``None`` when UNSTATED.

    Precedence: the env override, then the file, then unstated. An
    unreadable file reads as unstated rather than as empty, for the reason
    in the module docstring.
    """
    raw = env.get(SELECTION_ENV)
    if raw is not None and raw.strip():
        return parse_selection(raw)
    try:
        body = selection_path(home).read_text(encoding="utf-8")
    except OSError:  # stx-allow: fallback (reason: an absent or unreadable selection file means UNSTATED, a third state the caller must be able to tell from "empty")
        return None
    return parse_selection(body)


def is_selected(name: str, chosen: frozenset[str] | None) -> bool:
    """True when ``name`` is selected to run here.

    An UNSTATED selection answers True for every job: arriving machinery
    must not disarm a host that never opted into it. Both the local and
    the canonical spelling match, so a name copied out of ``--json`` or
    off a unit filename works as typed.
    """
    if chosen is None or SELECT_ALL in chosen:
        return True
    return name in chosen or _names.local(name) in chosen


def explain(chosen: frozenset[str] | None) -> str:
    """One line describing the selection, for the CLI to print.

    A knob whose current setting cannot be read back is a knob nobody
    trusts, so every command that consults the selection says what it
    found.
    """
    if chosen is None:
        return (
            "run-selection: UNSTATED (no "
            + SELECTION_ENV
            + ", no ~/"
            + SELECTION_FILE
            + ") — every declared job is eligible here"
        )
    if SELECT_ALL in chosen:
        return f"run-selection: {SELECT_ALL} — every declared job is eligible here"
    if not chosen:
        return (
            "run-selection: EMPTY — this host has deliberately selected NO "
            "jobs; nothing will be armed"
        )
    return "run-selection: " + ", ".join(sorted(chosen))


__all__ = [
    "SELECTION_ENV",
    "SELECTION_FILE",
    "SELECT_ALL",
    "explain",
    "is_selected",
    "parse_selection",
    "selection",
    "selection_path",
]
