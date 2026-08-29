"""EXACTLY ONE SUPERVISOR, AND IT IS ACTUALLY SUPERVISING.

"The new unit exists" is not the claim. "ONLY the new unit exists" is not the
whole claim either. A migration that installed the new name and left the old one
behind would report success while doubling the supervision of the fleet's
credential machinery, which is the precise failure this whole change is written
to prevent. So verification counts BOTH names and treats any survivor of the old
one as a failure, however healthy the new one looks.

WHY ARMING IS PART OF THE CLAIM (measured 2026-08-19, on the real cutover)
=========================================================================
``sac dev migrate-job-names --only accounts-refresh --include-held --yes`` ran
all ten steps green on scitex-compute-04 and left the fleet's OAuth refresher
**installed and disabled**::

    scitex-agent-container-accounts-keepalive.timer   enabled   enabled
    scitex-agent-container-accounts-refresh.timer     disabled  enabled

The old timer had been ``enabled`` and firing; the new one was not enabled at
all and did not appear in ``list-timers``. For the window between the two, the
sole-refresher host had NO refresher — and the verification pass said nothing,
because counting unit FILES cannot see it. Zero active supervisors satisfies
"not two supervisors" perfectly.

That is §2's *a gate that cannot fail is not a gate*: the check ran, on time, in
writing, and could not have caught the one outcome that mattered. The rule that
does catch it is the definition of a correct rename:

    **A RENAME MUST PRESERVE ARMING.** If the old unit was enabled before the
    cutover, the new unit must be enabled after it. If it was disabled, it stays
    disabled.

Stated that way it needs no policy input and no allow-list. It reads the host's
own pre-state as the expectation, which is why it flags accounts-refresh (old
was enabled) and stays silent about heal-agent-auth (old was deliberately
disabled — it is mutually exclusive with restart-login-expired-agents, and
"enable exactly one" is its own unit description's instruction). A guard that
demanded "every timer must be enabled" would have been wrong about the second
one, and being wrong about a deliberate disablement is how a guard teaches
people to ignore it.

ARMING IS THREE-VALUED, and collapsing it is the bug §2 warns about
===================================================================
A host may not be readable, or a caller may not have measured enablement at all.
"I could not tell" must not render as "armed correctly" — that is exactly the
unknown-collapsed-into-a-pole failure — and it must not render as a FAILURE
either, or every caller that does not measure starts reporting false alarms.
So ``armed_ok`` is ``True`` / ``False`` / ``None``, and ``None`` prints as NOT
CHECKED. ``ok`` keeps its original, narrower meaning — the installation claim —
so a caller that never looked at arming behaves exactly as before.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Callable, Iterable

from ._renames import Rename


def _suffix(unit: str, name: str) -> str:
    """The ``.service``/``.timer`` tail of ``unit`` under job ``name``."""
    return unit[len(name) :] if unit.startswith(name) else unit


@dataclass(frozen=True)
class Supervisors:
    """What is supervising one job after its cutover, and whether it is armed."""

    job: str
    expected: tuple[str, ...]
    found_new: tuple[str, ...]
    found_old: tuple[str, ...]
    #: New units that MUST be armed, because their old counterpart was.
    should_be_armed: tuple[str, ...] = ()
    #: New units observed armed. ``None`` means enablement was never measured.
    found_armed: tuple[str, ...] | None = None

    @property
    def ok(self) -> bool:
        """True only when the new set is complete and NOTHING old survives.

        Deliberately unchanged: this is the INSTALLATION claim. Arming is a
        separate signal so that a caller which never measured it is not
        silently told its migration passed a check it did not run.
        """
        return not self.found_old and set(self.found_new) == set(self.expected)

    @property
    def unarmed(self) -> tuple[str, ...]:
        """Units that were armed before the cutover and are not armed now."""
        if self.found_armed is None:
            return ()
        return tuple(u for u in self.should_be_armed if u not in self.found_armed)

    @property
    def armed_ok(self) -> bool | None:
        """True / False / ``None`` when enablement was not measured."""
        if self.found_armed is None:
            return None
        return not self.unarmed

    @property
    def verdict(self) -> str:
        """One line an operator can act on, naming what was actually found."""
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
        if not self.ok:
            missing = sorted(set(self.expected) - set(self.found_new))
            return f"FAIL {self.job}: NO supervisor — missing {missing}"
        if self.armed_ok is False:
            units = list(self.unarmed)
            return (
                f"FAIL {self.job}: INSTALLED BUT NOT ARMED — {units} "
                "was enabled before the cutover and is not now, so this job is "
                "supervised by nothing. Fix: systemctl --user enable --now "
                + " ".join(units)
            )
        if self.armed_ok is None:
            return (
                f"OK   {self.job}: exactly one supervisor {list(self.found_new)} "
                "(arming NOT CHECKED — caller measured no enablement)"
            )
        return (
            f"OK   {self.job}: exactly one supervisor {list(self.found_new)}, armed"
        )


def verify_exactly_one(
    rename: Rename,
    *,
    present: Iterable[str],
    armed_before: Iterable[str] | None = None,
    armed_now: Iterable[str] | None = None,
) -> Supervisors:
    """Count the supervisors for one job, and check the rename preserved arming.

    ``armed_before`` is the set of enabled units observed BEFORE the plan ran —
    it has to be captured then, because the plan's own ``disable`` step destroys
    the evidence. Passing neither set leaves arming unmeasured (``None``) rather
    than assumed.
    """
    on_host = frozenset(present)
    new_units = rename.new_units()

    if armed_before is None or armed_now is None:
        should: tuple[str, ...] = ()
        found_armed: tuple[str, ...] | None = None
    else:
        was = frozenset(armed_before)
        now = frozenset(armed_now)
        # Map old -> new by unit SUFFIX rather than by position, so the pairing
        # survives any future change to KIND_UNIT_SUFFIXES' ordering. This is
        # also what keeps the rule honest for a timer job: only the .timer is
        # ever enabled (the .service is static), so only the .timer can appear
        # here — the data decides, not a hardcoded suffix.
        armed_suffixes = {
            _suffix(u, rename.old) for u in rename.old_units() if u in was
        }
        should = tuple(u for u in new_units if _suffix(u, rename.new) in armed_suffixes)
        found_armed = tuple(u for u in new_units if u in now)

    return Supervisors(
        job=rename.new,
        expected=new_units,
        found_new=tuple(u for u in new_units if u in on_host),
        found_old=tuple(u for u in rename.old_units() if u in on_host),
        should_be_armed=should,
        found_armed=found_armed,
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
