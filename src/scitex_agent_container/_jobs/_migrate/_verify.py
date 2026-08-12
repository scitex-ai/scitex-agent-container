"""EXACTLY ONE SUPERVISOR — the claim the migration exists to make good on.

"The new unit exists" is not the claim. "ONLY the new unit exists" is.
A migration that installed the new name and left the old one behind would
report success while doubling the supervision of the fleet's credential
machinery, which is the precise failure this whole change is written to
prevent. So verification counts BOTH names and treats any survivor of the
old one as a failure, however healthy the new one looks.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Callable, Iterable

from ._renames import Rename


@dataclass(frozen=True)
class Supervisors:
    """What is supervising one job after its cutover."""

    job: str
    expected: tuple[str, ...]
    found_new: tuple[str, ...]
    found_old: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True only when the new set is complete and NOTHING old survives."""
        return not self.found_old and set(self.found_new) == set(self.expected)

    @property
    def verdict(self) -> str:
        """One line an operator can act on, naming what was actually found."""
        if self.ok:
            return f"OK   {self.job}: exactly one supervisor {list(self.found_new)}"
        if self.found_old and self.found_new:
            return (
                f"FAIL {self.job}: TWO SUPERVISORS — old {list(self.found_old)} "
                f"and new {list(self.found_new)} are both present"
            )
        if self.found_old:
            return (
                f"FAIL {self.job}: still on the OLD name {list(self.found_old)}; "
                "the cutover did not happen"
            )
        missing = sorted(set(self.expected) - set(self.found_new))
        return f"FAIL {self.job}: NO supervisor — missing {missing}"


def verify_exactly_one(rename: Rename, *, present: Iterable[str]) -> Supervisors:
    """Count the supervisors for one job against the host's unit files."""
    on_host = frozenset(present)
    return Supervisors(
        job=rename.new,
        expected=rename.new_units(),
        found_new=tuple(u for u in rename.new_units() if u in on_host),
        found_old=tuple(u for u in rename.old_units() if u in on_host),
    )


def systemd_user_available(*, which: Callable[[str], str | None] = shutil.which) -> bool:
    """True when this host can supervise ``--user`` units at all.

    MEASURED 2026-08-11: nas-01 (armv7l) and nas-02 have no ``systemctl``
    at all, and mba uses launchd — so ``service`` and ``timer`` are
    unimplementable on three of the fleet's nine hosts.

    Without this probe the migration would run its whole plan there, fail
    every ``systemctl`` call, and still reach ``verify`` — reporting "NO
    supervisor" for a host that was never going to have one, which reads
    as a broken migration rather than an inapplicable one. Refusing up
    front says the true thing.

    NOTE, because the brief that commissioned this work assumed otherwise:
    ``sac dev`` did NOT already refuse on those hosts. There is no
    ``systemctl`` probe anywhere in ``_dev_jobs.py`` or
    ``_dev_jobs_backend.py`` — the only refusal either can emit is about
    the installed scitex-dev's capabilities, never the host's, and
    ``manual_hint`` will still cheerfully print a ``systemctl`` line on a
    QNAP. This function is that missing guard, wired into the one verb
    here that mutates.
    """
    return which("systemctl") is not None


__all__ = ["Supervisors", "systemd_user_available", "verify_exactly_one"]
