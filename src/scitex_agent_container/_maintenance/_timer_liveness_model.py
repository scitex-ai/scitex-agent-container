"""Will this systemd --user timer EVER FIRE AGAIN? Three answers, never two.

THE FAULT THIS OBSERVES
-----------------------
A ``systemd --user`` timer rendered from ``OnBootSec=`` + ``OnUnitActiveSec=``
alone has TWO MONOTONIC triggers and no wall-clock one. ``OnBootSec`` elapses
exactly once. ``OnUnitActiveSec`` resolves to *<service last activation> +
interval*, so the first time the service misses a period the next elapse is
computed IN THE PAST — systemd marks the timer ``elapsed`` and does not fire
monotonic timers retroactively. ``Persistent=true`` does not rescue it:
systemd applies that only to ``OnCalendar=`` timers.

The timer is then dead forever, and EVERY instrument a person reaches for
says it is healthy::

    UnitFileState=enabled        ActiveState=active
    Result=success               SubState=elapsed
    NextElapseUSecMonotonic=infinity   <- the only field that tells the truth

MEASURED 2026-09-02 on scitex-compute-04: six enabled, active, ``success``
user timers in exactly that state — ``restart-login-expired-agents`` (last run
2026-08-28), ``fleet-reconcile`` (2026-08-28), ``freshness-refresh``,
``host-sync-check``, ``spartan-sif-bake`` (2026-08-20) and ``worktree-gc``
(2026-08-19). The operator noticed before any instrument did.

UPTIME IS THE RISK FACTOR AND A REBOOT MASKS IT. A reboot re-arms
``OnBootSec``, so a recently-rebooted host reads clean no matter how broken
its rendering is. compute-04 had been up since 2026-08-26. Do not read a
green answer on a fresh host as evidence the class is absent.

THREE-VALUED, AND THE THIRD VALUE IS LOAD-BEARING
-------------------------------------------------
* :data:`TIMER_ARMED` — a finite next elapse exists (monotonic or realtime),
  or the unit is mid-run.
* :data:`TIMER_NEVER_AGAIN` — monotonic ``infinity`` AND no realtime next AND
  ``SubState`` is not ``running``. The fault above.
* :data:`TIMER_UNKNOWN` — the properties could not be read. A host sac cannot
  query is NOT a pass; folding it into ARMED would make this quietest exactly
  when it is blindest.

A UNIT THAT DOES NOT EXIST READS EXACTLY LIKE A DEAD ONE, and that is why
``LoadState`` is read too. Measured on scitex-compute-04, 2026-09-02::

    $ systemctl --user show no-such-unit.timer -p ...
    NextElapseUSecRealtime=
    NextElapseUSecMonotonic=infinity
    SubState=dead                       <- rc 0, no error, no diagnostic

Every field the fault is defined by, from a unit systemd has never heard of.
A ``LoadState`` other than ``loaded`` (``not-found``, ``masked``) is therefore
UNKNOWN — sac was not shown a timer, so it has nothing to judge — rather than
an accusation against a unit that is not there. It cannot happen on the live
path, whose names come from ``list-unit-files``, and it happens the moment
anyone hands this a name by hand or a unit is removed mid-pass.

``SubState=running`` IS AN EXEMPTION, NOT AN OVERSIGHT. A timer whose service
is mid-run legitimately reports ``NextElapseUSecMonotonic=infinity`` — there
is no next elapse *while the current one is still executing*. Observed on
``docs-intake.timer``. Reporting that as dead would make the detector cry
wolf at precisely the units that are working hardest.

READING THE PROPERTIES, as this systemd actually prints them
------------------------------------------------------------
``systemctl show`` PRETTY-PRINTS both fields on systemd >= 250, so a parser
that expects raw microseconds reads every host wrong. Measured on
scitex-compute-04, 2026-09-02::

    NextElapseUSecMonotonic=6d 9h 11min 46.718290s   (armed, monotonic)
    NextElapseUSecRealtime=                          (no realtime next)

    NextElapseUSecMonotonic=0                        (no monotonic next)
    NextElapseUSecRealtime=Thu 2026-09-03 06:40:00 JST   (armed, calendar)

A monotonic value is an ABSOLUTE point on the monotonic clock rendered as
time-since-boot, not a countdown — which is why "6d 9h" appears on a
five-minute timer on a host up six days. This module therefore asks only
whether a next elapse EXISTS, never how far away it is; systemd itself sets
the field to ``infinity`` exactly when there is none.

The reading half (enumerate, run ``systemctl``, fold into a verdict) lives in
:mod:`._timer_liveness_probe`. Everything here is pure.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ARMING_OK",
    "ARMING_UNKNOWN",
    "ARMING_VIOLATION",
    "SCOPE_NOTE",
    "SHOW_PROPERTIES",
    "TIMER_ARMED",
    "TIMER_NEVER_AGAIN",
    "TIMER_UNKNOWN",
    "TimerLivenessVerdict",
    "TimerReading",
    "parse_show_output",
]

#: A finite next elapse exists, or the unit is mid-run. This timer will fire.
TIMER_ARMED = "armed"
#: Monotonic infinity, no realtime next, not running — it will NEVER fire again.
TIMER_NEVER_AGAIN = "never-again"
#: The properties could not be read. NOT a soft pass.
TIMER_UNKNOWN = "unknown"

#: Every examined timer is armed.
ARMING_OK = "ok"
#: At least one enabled timer will never fire again.
ARMING_VIOLATION = "violation"
#: No timer is dead, but at least one could not be read — so the invariant was
#: not asserted. Distinct from OK on purpose.
ARMING_UNKNOWN = "unknown"

_ID = "Id"
_LOAD_STATE = "LoadState"
_MONOTONIC = "NextElapseUSecMonotonic"
_REALTIME = "NextElapseUSecRealtime"
_SUB_STATE = "SubState"

#: The properties this check reads, in the order it asks systemd for them.
SHOW_PROPERTIES = (_ID, _LOAD_STATE, _MONOTONIC, _REALTIME, _SUB_STATE)

#: Values that mean "there is NO next elapse on this clock". ``0`` is what
#: systemd prints for the clock a timer does not use at all (a calendar-only
#: timer has monotonic ``0``); ``infinity`` is what it prints when a clock the
#: timer DOES use has run out of future. The raw ``USEC_INFINITY`` integer is
#: kept for older systemd that does not pretty-print.
_NO_NEXT = frozenset({"", "0", "infinity", "n/a", "18446744073709551615"})

#: The value that specifically means "this clock has run out of future".
_INFINITY = frozenset({"infinity", "18446744073709551615"})

#: The limit of this check, stated in every result rather than left to be
#: discovered. ``systemctl --user`` needs a user bus, and inside an apptainer
#: container there is none — measured 2026-09-02, ``systemctl --user show``
#: answers ``Failed to connect to bus: No medium found`` while
#: ``list-unit-files`` still reads the unit files off disk and succeeds. So a
#: container run enumerates timers and then learns NOTHING about any of them:
#: every reading is UNKNOWN. That is reported as UNKNOWN and never as OK.
SCOPE_NOTE = (
    "HOST-SCOPED and per-USER: this reads the systemd --user manager of the "
    "invoking user on THIS host. Run it on the HOST, not inside a container "
    "- a container has no user bus, so `systemctl --user show` fails while "
    "`list-unit-files` still succeeds and every reading comes back unknown. "
    "A recently REBOOTED host also reads clean regardless: a reboot re-arms "
    "OnBootSec, so uptime is the risk factor and a reboot masks it."
)


def parse_show_output(text: str) -> dict[str, str]:
    """``Key=Value`` lines to a dict, order-independent.

    Order-independent on purpose: systemd does NOT return the properties in
    the order they were asked for (measured — ``-p Id -p Next...`` comes back
    Realtime, Monotonic, Id, SubState), so a positional reader would parse the
    fields into each other's slots.

    A line with no ``=`` is dropped; it is a diagnostic (``Failed to connect
    to bus: ...``), not a property, and keeping it would let an error message
    masquerade as data. An EMPTY value is kept — ``NextElapseUSecRealtime=``
    is a real reading meaning "no realtime next", and is not the same fact as
    the property being absent.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip()
    return out


def _has_next(raw: str | None) -> bool:
    """True when this clock names a next elapse."""
    return raw is not None and raw.strip().lower() not in _NO_NEXT


def _is_infinity(raw: str | None) -> bool:
    """True when this clock has explicitly run out of future."""
    return raw is not None and raw.strip().lower() in _INFINITY


@dataclass(frozen=True)
class TimerReading:
    """One enabled timer, as read. ``None`` means the property was ABSENT.

    Absent and empty are kept apart because they are different facts:
    ``NextElapseUSecRealtime=`` is systemd answering "no realtime next", while
    a missing key means sac never got an answer at all — the first is data,
    the second is blindness.
    """

    unit: str
    load_state: str | None = None
    monotonic: str | None = None
    realtime: str | None = None
    sub_state: str | None = None
    #: Why this reading is unusable, when it is. Empty for a usable reading.
    read_error: str = ""

    @classmethod
    def from_show_output(cls, unit: str, text: str) -> "TimerReading":
        """Build a reading from one ``systemctl show`` capture.

        ``unit`` is a fallback: when the capture carries its own ``Id=`` that
        wins, because the name systemd answers with is the unit that was
        really inspected — an alias or a filename typo would otherwise be
        recorded under the name that was ASKED for.
        """
        props = parse_show_output(text)
        name = props.get(_ID) or unit
        missing = [p for p in (_MONOTONIC, _REALTIME, _SUB_STATE) if p not in props]
        error = ""
        if missing:
            stripped = text.strip()
            head = stripped.splitlines()[0].strip() if stripped else ""
            error = f"missing {', '.join(missing)}"
            if not stripped:
                error = f"{error} (systemctl returned nothing)"
            elif "=" not in head:
                error = f"{error} (systemctl said: {head})"
        return cls(
            unit=name,
            load_state=props.get(_LOAD_STATE),
            monotonic=props.get(_MONOTONIC),
            realtime=props.get(_REALTIME),
            sub_state=props.get(_SUB_STATE),
            read_error=error,
        )

    @property
    def state(self) -> str:
        """ARMED / NEVER_AGAIN / UNKNOWN — the whole classification.

        Pure, and deliberately reachable without a systemd: recorded captures
        exercise this exact function, so the rule can be asserted against a
        real elapsed timer's output without having to break one.
        """
        if self.read_error:
            return TIMER_UNKNOWN
        # Not a loaded unit => not a timer sac was shown, so there is
        # nothing to judge. Measured: a name systemd has never heard of
        # answers rc 0 with infinity + SubState=dead, which is the fault's
        # exact signature.
        if self.load_state is not None and self.load_state.strip().lower() != "loaded":
            return TIMER_UNKNOWN
        # A running service legitimately has no next elapse WHILE it runs.
        if (self.sub_state or "").strip().lower() == "running":
            return TIMER_ARMED
        if _has_next(self.monotonic) or _has_next(self.realtime):
            return TIMER_ARMED
        if _is_infinity(self.monotonic) and not _has_next(self.realtime):
            return TIMER_NEVER_AGAIN
        # No next elapse on either clock, and monotonic is not the infinity
        # sentinel either. That is a reading this rule does not recognise, and
        # an unrecognised reading is UNKNOWN rather than a guessed verdict.
        return TIMER_UNKNOWN

    @property
    def why(self) -> str:
        """One line saying what the classification was read off."""
        if self.read_error:
            return self.read_error
        loaded = (self.load_state or "loaded").strip().lower()
        if loaded != "loaded":
            return (
                f"{_LOAD_STATE}={self.load_state} - not a loaded unit, so "
                "there is no timer here to judge"
            )
        state = self.state
        if state == TIMER_ARMED:
            if (self.sub_state or "").strip().lower() == "running":
                return "SubState=running - mid-run, so no next elapse yet"
            monotonic = _has_next(self.monotonic)
            clock = "monotonic" if monotonic else "realtime"
            value = self.monotonic if monotonic else self.realtime
            return f"next elapse on the {clock} clock: {value}"
        if state == TIMER_NEVER_AGAIN:
            return (
                f"{_MONOTONIC}={self.monotonic}, no realtime next, "
                f"{_SUB_STATE}={self.sub_state}"
            )
        return (
            f"unrecognised reading: {_MONOTONIC}={self.monotonic!r}, "
            f"{_REALTIME}={self.realtime!r}"
        )

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "unit": self.unit,
            "state": self.state,
            "load_state": self.load_state,
            "why": self.why,
            "monotonic": self.monotonic,
            "realtime": self.realtime,
            "sub_state": self.sub_state,
            "read_error": self.read_error,
        }


@dataclass(frozen=True)
class TimerLivenessVerdict:
    """The host-wide verdict. ``state`` is OK / VIOLATION / UNKNOWN."""

    state: str
    readings: tuple[TimerReading, ...] = ()
    #: False when the ENUMERATION itself could not run — the reason a zero
    #: count must not read as OK.
    scanned: bool = True
    source: str = "systemctl --user"
    detail: str = ""

    def _named(self, state: str) -> tuple[str, ...]:
        return tuple(r.unit for r in self.readings if r.state == state)

    @property
    def armed(self) -> tuple[str, ...]:
        """Units with a finite next elapse (or mid-run)."""
        return self._named(TIMER_ARMED)

    @property
    def never_again(self) -> tuple[str, ...]:
        """The offenders — enabled units that will never fire again."""
        return self._named(TIMER_NEVER_AGAIN)

    @property
    def unknown(self) -> tuple[str, ...]:
        """Units sac could not read. Never counted as armed."""
        return self._named(TIMER_UNKNOWN)

    @property
    def armed_count(self) -> int:
        """How many examined timers will fire again."""
        return len(self.armed)

    @property
    def never_again_count(self) -> int:
        """How many examined timers are dead."""
        return len(self.never_again)

    @property
    def unknown_count(self) -> int:
        """How many examined timers sac could not classify."""
        return len(self.unknown)

    @property
    def is_alarming(self) -> bool:
        """True for the two states a human must be told about."""
        return self.state in (ARMING_VIOLATION, ARMING_UNKNOWN)

    @property
    def exit_code(self) -> int:
        """Non-zero when a timer is dead — the gate the incident needed.

        VIOLATION is 1. UNKNOWN is 2 rather than 0 because a vantage sac could
        not query is not a host that passed, and a caller that must tell the
        two apart (a dead timer here vs a probe that never ran) can.
        """
        if self.state == ARMING_VIOLATION:
            return 1
        if self.state == ARMING_UNKNOWN:
            return 2
        return 0

    def population(self) -> str:
        """What was actually examined. Never let a clean count stand alone.

        "0 dead timers" means nothing until the same reading says how many
        were looked at: ``0 of 0 examined`` and ``0 of 22`` are different
        facts and must not render the same.
        """
        return (
            f"{len(self.readings)} enabled timer(s) examined, "
            f"{self.armed_count} armed, {self.never_again_count} never-again, "
            f"{self.unknown_count} unreadable"
        )

    def summary(self) -> str:
        """One-line human summary of the verdict."""
        if self.state == ARMING_VIOLATION:
            names = ", ".join(self.never_again)
            return (
                f"{self.never_again_count} enabled timer(s) will NEVER fire "
                f"again: {names}"
            )
        if self.state == ARMING_UNKNOWN:
            if not self.scanned:
                return "unknown - the timer enumeration could not run"
            return (
                f"unknown - {self.unknown_count} of {len(self.readings)} "
                "enabled timer(s) would not yield a next elapse"
            )
        return f"{self.armed_count} enabled timer(s), all armed"

    def hint(self) -> str:
        """What to DO. Empty for OK — an all-clear needs no remedy.

        The violation branch leads with a question rather than a command, and
        that ordering is the point: sac's per-leaf timer lowering is RETIRED,
        hosts still carry ORPHAN units from it, and re-arming one of those
        puts a SECOND scheduler on a job the ecosystem supervisor already
        runs. A hint that said "re-arm it" would hand out the double-supervisor
        hazard as a remedy.
        """
        if self.state == ARMING_VIOLATION:
            names = ", ".join(self.never_again)
            return (
                "These units are enabled, active and Result=success, and they "
                f"are dead: {names}. FIRST ask whether the unit should exist "
                "at all - sac's per-leaf timer lowering is RETIRED and hosts "
                "still carry ORPHAN <job>.timer units from it; re-arming one "
                "of those puts a SECOND scheduler on a job the ecosystem "
                "supervisor already runs, with independent debounce state. An "
                "orphan wants `systemctl --user disable --now <unit>`, not "
                "reviving. For a unit that IS still the real scheduler, "
                "restarting the TIMER alone does NOT re-arm it (verified on "
                "all six): with no service activation to anchor "
                "OnUnitActiveSec the next elapse is still computed in the "
                "past. Start the SERVICE first, then restart the timer - "
                "`systemctl --user start <name>.service && systemctl --user "
                "restart <name>.timer` - and confirm NextElapseUSecMonotonic "
                "came back finite. The permanent fix is a wall-clock trigger "
                "(OnCalendar= with Persistent=true), which cannot land in the "
                "past-forever state and catches up a missed run."
            )
        if self.state == ARMING_UNKNOWN:
            if not self.scanned:
                detail = self.detail or "no detail"
                return (
                    f"The enumeration could not run ({detail}), so NOTHING "
                    "was learned - this is not an all-clear. Re-run on a Linux "
                    "host with a live `systemd --user` manager."
                )
            names = ", ".join(self.unknown)
            return (
                f"sac could not read a next elapse for: {names}. The usual "
                "cause is VANTAGE - `systemctl --user show` needs a user bus, "
                "and inside a container there is none (`Failed to connect to "
                "bus: No medium found`) while `list-unit-files` still succeeds "
                "off disk, so timers are enumerated and then nothing is "
                "learned about them. Re-run on the HOST as the user that owns "
                "these units before believing any row."
            )
        return ""

    def to_dict(self) -> dict:
        """JSON-friendly projection, matching the doctor's other checks."""
        return {
            "state": self.state,
            "scope": "host",
            "scope_note": SCOPE_NOTE,
            "source": self.source,
            "examined": len(self.readings),
            "armed": list(self.armed),
            "never_again": list(self.never_again),
            "unknown": list(self.unknown),
            "armed_count": self.armed_count,
            "never_again_count": self.never_again_count,
            "unknown_count": self.unknown_count,
            "timers": [r.to_dict() for r in self.readings],
            "scanned": self.scanned,
            "exit_code": self.exit_code,
            "population": self.population(),
            "detail": self.detail,
            "summary": self.summary(),
            "hint": self.hint(),
        }
