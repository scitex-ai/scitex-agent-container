"""Plan the ``to_home_layers`` sweep over every registered spec — no writes.

The migration adds one declaration line to ~101 hand-maintained ``spec.yaml``
files. The constitution's rule for a bulk operation is that you read what it
WOULD do before it does anything, and that the dry-run prints the counts and
the affected set rather than a summary you are asked to trust. This module is
that dry-run, as a value: :func:`plan_migration` performs no I/O beyond reading,
and the object it returns is what the applying half consumes.

Three things it refuses to blur:

* A spec whose shape the editor does not recognise is REFUSED and named, not
  silently skipped. Skipping is how a sweep reports "101 done" over a fleet of
  102 and nobody notices the one.
* A planned edit that would change more than a single line is a DEFECT, not a
  bigger success. The plan carries that per spec, so the apply can refuse
  before touching a file rather than after.
* A roster that was never SEARCHED is not a fleet with nothing to do. "0 of 0"
  is the limit case of the first bullet — a sweep reporting completion over a
  population it never saw — and it is the one this module shipped wrong; see
  :mod:`._roster_state`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._roster_state import RosterState


@dataclass(frozen=True)
class SpecEdit:
    """One spec's planned edit. ``new_text`` is None when nothing will be written."""

    agent: str
    path: Path
    layers: "tuple[str, ...]"
    new_text: "str | None" = None
    #: Why this spec will NOT be written. None means it will be.
    refusal: "str | None" = None
    #: How many lines differ between the original and ``new_text``. Anything
    #: other than 1 means the editor did something unintended.
    lines_added: int = 0

    @property
    def will_write(self) -> bool:
        return self.new_text is not None and self.refusal is None


@dataclass(frozen=True)
class MigrationPlan:
    """What the sweep would do, in full, before it does any of it."""

    edits: "tuple[SpecEdit, ...]" = ()
    #: Specs found on disk that could not be parsed into an edit at all.
    unreadable: "tuple[str, ...]" = field(default=())
    #: What the roster this plan was built from actually WAS — absent, empty or
    #: populated. None when the caller did not say, which keeps a plan built
    #: from an explicit list of edits (tests, callers holding their own paths)
    #: judged exactly as before.
    roster: "RosterState | None" = None

    @property
    def writable(self) -> "tuple[SpecEdit, ...]":
        return tuple(e for e in self.edits if e.will_write)

    @property
    def refused(self) -> "tuple[SpecEdit, ...]":
        return tuple(e for e in self.edits if e.refusal is not None)

    @property
    def malformed(self) -> "tuple[SpecEdit, ...]":
        """Planned writes that would touch other than exactly one line.

        Separated from ``refused`` deliberately: a refusal is the editor
        declining a shape it does not know, which is correct behaviour. This is
        the editor accepting a shape and then producing a diff nobody asked
        for, which is a bug. Conflating them would let a real defect be read as
        "one more spec needing manual attention".
        """
        return tuple(e for e in self.writable if e.lines_added != 1)

    @property
    def safe_to_apply(self) -> bool:
        """True only when every planned write is a clean single-line insert.

        Refusals do NOT make a plan unsafe — a named, counted refusal is a
        legitimate outcome that a human resolves. A malformed edit or an
        unreadable spec does, because both mean the plan does not describe what
        would actually happen.

        AN UNSEARCHED ROSTER DOES TOO, and it is the one this property used to
        get wrong. With no specs discovered, ``malformed`` and ``unreadable``
        are both trivially empty and this returned True — sound, over nothing.
        A plan that describes no specs does not describe the sweep any more than
        one that cannot describe a spec it found; see :mod:`._roster_state`.
        """
        if self.roster is not None and not self.roster.is_populated:
            return False
        return not self.malformed and not self.unreadable

    def summary(self) -> str:
        # The roster line comes FIRST and alone: when the roster was not
        # searched, "0 spec(s) would be written" is not a smaller truth to
        # append to, it is the misreading itself.
        if self.roster is not None and not self.roster.is_populated:
            return self.roster.describe()
        parts = [f"{len(self.writable)} spec(s) would be written"]
        if self.refused:
            parts.append(
                f"{len(self.refused)} REFUSED ("
                + ", ".join(sorted(e.agent for e in self.refused))
                + ")"
            )
        if self.malformed:
            parts.append(f"{len(self.malformed)} MALFORMED — do not apply")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} unreadable")
        return "; ".join(parts)


def count_added_lines(before: str, after: str) -> int:
    """Lines present in ``after`` and not in ``before``, positionally.

    A set difference would call a moved line "unchanged" and a duplicated line
    "no change at all", which is precisely the damage a bulk YAML edit does.
    Comparing sequences keeps a reordering visible as what it is.
    """
    b, a = before.splitlines(), after.splitlines()
    if len(a) < len(b):
        return len(a) - len(b)
    i = j = added = 0
    while i < len(b) and j < len(a):
        if b[i] == a[j]:
            i += 1
            j += 1
        else:
            added += 1
            j += 1
    return added + (len(a) - j)


__all__ = ["MigrationPlan", "SpecEdit", "count_added_lines"]
