"""Invoke the ``may-stop`` detector and read its verdict — in THREE states.

We do not implement the runnable-work predicate; scitex-cards owns it. This
module only runs it and reads the answer honestly.

The contract (scitex-cards)::

    exit 0  nothing runnable — stopping is allowed
    exit 2  runnable work exists; stdout = one-line JSON
            {"agent", "runnable": true,
             "items": [{"card_id", "reason", "next_action"}, ...],
             "idle_seconds": <int>}
            stderr = numbered hints "N. <card_id> — <reason> — <next_action>"

Two properties of the real deployment shape this module:

**stderr is dirty.** The live store prepends a ``SCITEX_TODO_*``
deprecation warning and one ``[scitex-todo] TOLERATED (read-side)`` block
per unknown-status card before any payload. So hints are matched by the
NUMBERED-LINE PATTERN anywhere in the stream — never by line position, and
never by "everything after the header".

**exit 2 is a positive signal.** If we get exit 2 but cannot parse a single
item out of either stream, work still EXISTS; we simply do not know its
details. That is :data:`RUNNABLE` with an empty item list, and it blocks.
Downgrading an unparseable exit 2 to :data:`ALLOW` would silently restore
the exact idle-with-work-pending state this package exists to make
unreachable.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field

#: Detector said: nothing runnable. Definite — stopping is allowed.
ALLOW = "allow"
#: Detector said: runnable work exists. Definite — the stop must be converted.
RUNNABLE = "runnable"
#: We COULD NOT TELL (missing / timed out / crashed / unexpected rc).
#: Distinct from :data:`ALLOW` on purpose: absence of evidence is not
#: evidence of absence. Fails open, but loudly.
UNKNOWN = "unknown"

#: Default detector command. Overridable via ``$SAC_MAY_STOP_CMD`` — both an
#: operator escape hatch (the command has moved before: the predecessor
#: shipped as ``python -m scitex_cards._idle_guard``) and the test seam that
#: lets a test point at a REAL script instead of patching a callable.
_DEFAULT_CMD = "scitex-cards may-stop"
_CMD_ENV = "SAC_MAY_STOP_CMD"

#: Seconds before we give up on the detector and fail open.
_TIMEOUT_S = 15.0

#: ``N. <card_id> — <reason> — <next_action>``. The separator is an em dash
#: per the contract; a plain hyphen surrounded by spaces is accepted too so
#: an ASCII-only emitter still parses. Card ids never contain the separator.
_HINT_RE = re.compile(
    r"^\s*\d+\.\s+(?P<card_id>.+?)\s+(?:—|--|\s-\s)\s*"
    r"(?P<reason>.+?)\s+(?:—|--|\s-\s)\s*(?P<next_action>.+?)\s*$"
)


@dataclass(frozen=True)
class RunnableItem:
    """One piece of runnable work the agent must take."""

    card_id: str
    reason: str = ""
    next_action: str = ""


@dataclass(frozen=True)
class Verdict:
    """The detector's answer: one of :data:`ALLOW` / :data:`RUNNABLE` /
    :data:`UNKNOWN`, plus whatever detail we could read."""

    state: str
    items: tuple[RunnableItem, ...] = ()
    idle_seconds: "int | None" = None
    detail: str = ""
    returncode: "int | None" = None

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(item.card_id for item in self.items)


def detector_argv(agent: str) -> list[str]:
    """Build the detector command line for ``agent``.

    Honours ``$SAC_MAY_STOP_CMD`` (split with :func:`shlex.split`, so it may
    carry its own flags). ``--agent <id>`` is always appended: the detector
    must be told WHO to answer for, never left to infer it.
    """
    base = (os.environ.get(_CMD_ENV) or "").strip() or _DEFAULT_CMD
    return [*shlex.split(base), "--agent", agent]


def _parse_stdout(text: str) -> "tuple[tuple[RunnableItem, ...], int | None]":
    """Read ``items`` + ``idle_seconds`` from the one-line JSON payload.

    Scans every line for a JSON object rather than assuming the payload is
    the whole of stdout — a warning printed to stdout must not cost us the
    structured items. Returns ``((), None)`` when nothing parses.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:  # stx-allow: fallback (reason: a noisy stdout line is not the payload; keep scanning)
            continue
        if not isinstance(data, dict) or "items" not in data:
            continue
        items: list[RunnableItem] = []
        raw_items = data.get("items")
        if isinstance(raw_items, list):
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                card_id = str(entry.get("card_id") or "").strip()
                if not card_id:
                    continue
                items.append(
                    RunnableItem(
                        card_id=card_id,
                        reason=str(entry.get("reason") or "").strip(),
                        next_action=str(entry.get("next_action") or "").strip(),
                    )
                )
        idle = data.get("idle_seconds")
        return tuple(items), idle if isinstance(idle, int) else None
    return (), None


def _parse_hints(text: str) -> tuple[RunnableItem, ...]:
    """Read hint lines from stderr BY PATTERN, ignoring position.

    Tolerated store read-warnings and the deprecation banner sit above the
    hints in the real stream, and their count varies with the store's
    contents — so anchoring on "the line after the header" or on a fixed
    offset would silently pick up warnings as work items.
    """
    items: list[RunnableItem] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _HINT_RE.match(line)
        if not match:
            continue
        card_id = match.group("card_id").strip()
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        items.append(
            RunnableItem(
                card_id=card_id,
                reason=match.group("reason").strip(),
                next_action=match.group("next_action").strip(),
            )
        )
    return tuple(items)


@dataclass
class _Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    failure: str = ""
    _unused: tuple = field(default=(), repr=False)


def _invoke(argv: list[str]) -> _Run:
    """Run the detector, converting every failure mode into a ``_Run``."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return _Run(
            returncode=-1,
            failure=f"detector not found: {argv[0]!r} is not on PATH",
        )
    except subprocess.TimeoutExpired:
        return _Run(
            returncode=-1,
            failure=f"detector timed out after {_TIMEOUT_S:.0f}s",
        )
    except OSError as exc:  # stx-allow: fallback (reason: detector spawn failure must fail open, not crash the agent's turn)
        return _Run(returncode=-1, failure=f"detector could not be run: {exc}")
    return _Run(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def probe(agent: str) -> Verdict:
    """Ask the detector whether ``agent`` may stop. Never raises.

    * ``rc == 0``  → :data:`ALLOW` (definite).
    * ``rc == 2``  → :data:`RUNNABLE`; items from stdout JSON, falling back
      to the stderr hints. An exit 2 whose detail we cannot parse is STILL
      :data:`RUNNABLE` — with no items — because the exit code already
      proved work exists.
    * anything else → :data:`UNKNOWN`, carrying a ``detail`` for the loud
      log.
    """
    if not agent:
        return Verdict(
            state=UNKNOWN,
            detail=(
                "could not resolve this agent's identity from the environment "
                "(no SCITEX_CARDS_AGENT_ID / SCITEX_TODO_AGENT_ID / SAC_NAME); "
                "refusing to guess it from the working directory"
            ),
        )

    argv = detector_argv(agent)
    run = _invoke(argv)

    if run.failure:
        return Verdict(state=UNKNOWN, detail=run.failure, returncode=run.returncode)

    if run.returncode == 0:
        return Verdict(state=ALLOW, returncode=0)

    if run.returncode == 2:
        items, idle = _parse_stdout(run.stdout)
        if not items:
            items = _parse_hints(run.stderr)
        detail = (
            ""
            if items
            else (
                "detector reported runnable work (exit 2) but neither its "
                "stdout JSON nor its stderr hints could be parsed; blocking "
                "the stop anyway because exit 2 already proved work exists"
            )
        )
        return Verdict(
            state=RUNNABLE,
            items=tuple(items),
            idle_seconds=idle,
            detail=detail,
            returncode=2,
        )

    tail = (run.stderr or run.stdout or "").strip().splitlines()
    return Verdict(
        state=UNKNOWN,
        detail=(
            f"detector exited {run.returncode} (expected 0 or 2)"
            + (f": {tail[-1][:300]}" if tail else "")
        ),
        returncode=run.returncode,
    )


__all__ = [
    "ALLOW",
    "RUNNABLE",
    "UNKNOWN",
    "RunnableItem",
    "Verdict",
    "detector_argv",
    "probe",
]
