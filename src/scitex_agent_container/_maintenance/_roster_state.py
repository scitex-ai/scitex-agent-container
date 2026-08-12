"""Is the spec roster ABSENT, EMPTY, or POPULATED? — three states, not two.

WHY THIS EXISTS. :func:`fleet_spec_paths` returns ``[]`` for a root that does
not exist and ``[]`` for a fleet with no specs in it. A sweep consuming that
list cannot tell "I looked nowhere" from "there was nothing to do", so it
reports the second when the first is true.

Measured 2026-08-10 inside an agent container, same host, seconds apart:

    sac agents migrate-layers --json
      {"specs": 0, "writable": 0, "unreadable": [], "safe_to_apply": true,
       "summary": "0 spec(s) would be written", "exit_code": 0}

    SCITEX_AGENT_CONTAINER_AGENTS_DIR=/home/ywatanabe/.scitex/agent-container/agents \\
    sac agents migrate-layers --json
      {"specs": 102, "writable": 102, "safe_to_apply": true, "exit_code": 0}

The fleet root is ``$HOME/.scitex/agent-container/agents``; ``$HOME`` is
``/home/agent`` inside a container and ``/home/ywatanabe`` on the host, so the
first run searched a directory that does not exist. It exited 0 and called the
plan sound. Worse, ``--apply`` down that path prints "every spec already
declares its layers ... this is what a completed one looks like" — the command
ASSERTS the migration is finished after looking nowhere.

THE THREE STATES, and they need three different responses:

    absent      the root is not a directory   -> wrong root, or nothing has ever
                                                 been registered here. No claim
                                                 about the fleet is licensed.
    empty       the root exists, no specs     -> a real, reportable fact: this
                                                 registry has no agents.
    populated   N specs found                 -> the only state in which "0 to
                                                 write" means the sweep is done.

Collapsing ``absent`` into ``empty`` is the defect. Only ``populated`` licenses
a factual claim about what the sweep would do.

Design note carried from :mod:`.._state.state_db_health` (``StoreState``): the
distinction belongs at the REPORTING boundary, not in the enumerator.
``fleet_spec_paths`` keeps returning a plain list — ``.._authheal._detect``
shares it and a raising enumerator would break a caller that is not reporting
to anyone. This is for the sweep that tells a human what it did.

A ROOT THAT EXISTS IS NOT A ROOT THAT IS RIGHT: ``/home/agent/.scitex`` exists
(it is a real directory, mode 0775) and only ``agent-container/agents`` beneath
it is missing. Any check that stops at "is the state root there" says yes. That
is why this inspects the SPEC DIRECTORY itself and counts what it found.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The three distinguishable states of a spec roster.
ROSTER_STATES = ("absent", "empty", "populated")


@dataclass(frozen=True)
class RosterState:
    """What the roster at ``root`` actually is, and what it licenses."""

    root: Path
    state: str
    n_specs: int = 0

    def __post_init__(self) -> None:
        if self.state not in ROSTER_STATES:
            raise ValueError(
                f"unknown roster state {self.state!r}; expected one of {ROSTER_STATES}"
            )

    @property
    def is_populated(self) -> bool:
        """True only when a count of zero may be reported as a fact."""
        return self.state == "populated"

    def describe(self) -> str:
        """One line naming the state AND the root, because the root is the bug.

        A message that says "0 specs" without saying where it looked is the
        message that let this ship: it is equally true of a finished migration
        and a total discovery failure.
        """
        if self.state == "absent":
            return (
                f"no spec roster at {self.root} — the directory does not exist, "
                f"so nothing was searched. This is NOT an empty fleet. Set "
                f"SCITEX_AGENT_CONTAINER_AGENTS_DIR to the registry you mean "
                f"(inside a container $HOME is not the host's)."
            )
        if self.state == "empty":
            return f"{self.root} exists but holds no specs — the registry is empty"
        return f"{self.n_specs} spec(s) under {self.root}"


def inspect_roster(root: "Path | None", spec_paths) -> RosterState:
    """Classify the roster ``spec_paths`` was enumerated from.

    ``spec_paths`` is passed in rather than re-globbed: re-enumerating could
    disagree with the list the plan was actually built from, and two answers
    about one fleet is the failure this module exists to prevent.

    ``root`` of None means the caller supplied paths directly (a test corpus,
    an explicit list). There is no directory to judge, so a non-empty list is
    populated and an empty one is empty — never ``absent``, because nothing was
    claimed to exist in the first place.
    """
    paths = list(spec_paths)
    if root is None:
        return RosterState(
            root=Path("(explicit paths)"),
            state="populated" if paths else "empty",
            n_specs=len(paths),
        )
    if not Path(root).is_dir():
        return RosterState(root=Path(root), state="absent", n_specs=0)
    if not paths:
        return RosterState(root=Path(root), state="empty", n_specs=0)
    return RosterState(root=Path(root), state="populated", n_specs=len(paths))


__all__ = ["ROSTER_STATES", "RosterState", "inspect_roster"]
