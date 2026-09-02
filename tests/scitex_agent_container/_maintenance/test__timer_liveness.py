"""Tests for the timer-liveness check — will an enabled --user timer ever fire?

PA-306: no ``unittest.mock``, no monkeypatching. Every case is a REAL file in
``tmp_path`` holding REAL recorded ``systemctl show`` output, classified by
the same :func:`check_timer_liveness` a live host goes through. The captures
are not invented shapes: all but ONE were taken verbatim from
scitex-compute-04 on 2026-09-02. The exception is the DEAD capture, and it is
labelled at its definition rather than passed off as a recording — all six
units were repaired that day, so no host still holds the shape; its fields are
the ones the incident measured on every one of them.

TWO PROPERTIES OF THE REAL OUTPUT THAT A HAND-WRITTEN FIXTURE WOULD MISS, and
each of them would break a plausible parser:

* systemd PRETTY-PRINTS both fields on this version — ``6d 9h 11min
  46.718290s``, not a microsecond integer — and a monotonic value is an
  absolute point on the monotonic clock rendered as time-since-boot, not a
  countdown. Hence "6d 9h" on a five-minute timer.
* it does NOT answer in the order the properties were asked for. The capture
  comes back Realtime, Monotonic, Id, LoadState, ActiveState, SubState.

The behaviours that matter:

* a monotonic-armed timer and a calendar-armed timer are both ARMED,
* the incident's shape — ``infinity`` + ``elapsed`` — is NEVER_AGAIN,
* ``infinity`` + ``SubState=running`` is ARMED, because a timer whose service
  is mid-run has no next elapse YET (observed on ``docs-intake.timer``); this
  is the exemption that stops the check crying wolf at the busiest units,
* a unit that DOES NOT EXIST answers with the fault's exact signature
  (``infinity`` + ``SubState=dead``, exit 0) and must not be accused,
* a capture sac could not read is UNKNOWN and NEVER passes,
* the verdict carries the counts and the offending unit NAMES,
* a dead timer OUTRANKS an unreadable one, so blindness cannot hide a finding,
* the exit code is non-zero when anything is dead.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._maintenance._timer_liveness_model import (
    ARMING_OK,
    ARMING_UNKNOWN,
    ARMING_VIOLATION,
    TIMER_ARMED,
    TIMER_NEVER_AGAIN,
    TIMER_UNKNOWN,
    TimerReading,
    parse_show_output,
)
from scitex_agent_container._maintenance._timer_liveness_probe import (
    check_timer_liveness,
    verdict_for,
)

# --- recorded captures ------------------------------------------------------
# Taken verbatim from scitex-compute-04, 2026-09-02, with
#   systemctl --user show <unit> -p Id -p LoadState -p NextElapseUSecMonotonic \
#       -p NextElapseUSecRealtime -p SubState -p ActiveState
# Property order is systemd's OWN reply order, deliberately NOT the order the
# -p flags asked for.

ARMED_MONOTONIC = """NextElapseUSecRealtime=
NextElapseUSecMonotonic=6d 9h 27min 23.966112s
Id=scitex-agent-container-fleet-reconcile.timer
LoadState=loaded
ActiveState=active
SubState=waiting
"""

# A calendar-driven timer: no monotonic next at all (`0`, not `infinity`),
# armed on the realtime clock. Proves `0` is not read as death.
ARMED_REALTIME = """NextElapseUSecRealtime=Thu 2026-09-03 06:40:00 JST
NextElapseUSecMonotonic=0
Id=dotfiles-drift-check.timer
LoadState=loaded
ActiveState=active
SubState=waiting
"""

# THE FAULT. The ONE capture here that is not verbatim, and it is labelled
# rather than passed off: all six units were REPAIRED on 2026-09-02 before this
# module existed, so no live host still holds the shape. The fields are the
# ones the incident measured on every one of them — enabled, ActiveState=active,
# Result=success, SubState=elapsed, NextElapseUSecMonotonic=infinity.
DEAD_ELAPSED = """NextElapseUSecRealtime=
NextElapseUSecMonotonic=infinity
Id=scitex-agent-container-restart-login-expired-agents.timer
LoadState=loaded
ActiveState=active
SubState=elapsed
Result=success
"""

# Same `infinity`, opposite meaning, and this one IS verbatim: docs-intake was
# mid-run at capture time. A timer whose service is executing has no next
# elapse until that run ends.
RUNNING_INFINITY = """NextElapseUSecRealtime=
NextElapseUSecMonotonic=infinity
Id=docs-intake.timer
LoadState=loaded
ActiveState=active
SubState=running
"""

# A unit systemd has never heard of, verbatim: rc 0, no diagnostic, and EVERY
# field the fault is defined by. LoadState is the only thing separating this
# from a genuinely dead timer.
NOT_FOUND = """NextElapseUSecRealtime=
NextElapseUSecMonotonic=infinity
Id=no-such-unit.timer
LoadState=not-found
SubState=dead
"""

# What `systemctl --user show` really answers with no user bus — measured
# inside this agent's container, 2026-09-02. No properties at all.
UNREADABLE = "Failed to connect to bus: No medium found\n"


def _capture(root: Path, unit: str, text: str) -> Path:
    """Write one recorded capture, exactly as ``systemctl show`` emitted it."""
    path = root / f"{unit}.txt"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def one_of_each(tmp_path: Path) -> Path:
    """A capture directory holding one of every classifiable shape."""
    root = tmp_path / "captures"
    root.mkdir()
    _capture(root, "scitex-agent-container-fleet-reconcile.timer", ARMED_MONOTONIC)
    _capture(root, "dotfiles-drift-check.timer", ARMED_REALTIME)
    _capture(
        root,
        "scitex-agent-container-restart-login-expired-agents.timer",
        DEAD_ELAPSED,
    )
    _capture(root, "docs-intake.timer", RUNNING_INFINITY)
    _capture(root, "launchpadlib-cache-clean.timer", UNREADABLE)
    return root


def test_parse_reads_properties_regardless_of_order() -> None:
    # Arrange — systemd answers Realtime, Monotonic, Id, SubState, which is
    # NOT the order `-p Id -p NextElapse...` asked for. A positional reader
    # would file every value under the wrong key.
    # Act
    props = parse_show_output(ARMED_MONOTONIC)
    # Assert
    assert props["Id"] == "scitex-agent-container-fleet-reconcile.timer"


def test_parse_keeps_an_empty_value_as_a_reading() -> None:
    # Arrange — `NextElapseUSecRealtime=` is systemd SAYING "no realtime
    # next". Dropping it would make that indistinguishable from never having
    # asked, which is the difference between data and blindness.
    # Act
    props = parse_show_output(ARMED_MONOTONIC)
    # Assert
    assert props["NextElapseUSecRealtime"] == ""


def test_pretty_printed_monotonic_reads_as_armed(tmp_path: Path) -> None:
    # Arrange — the value is `6d 9h 14min 43.711141s`, not microseconds. A
    # parser expecting an integer reads every host on this systemd wrong.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "scitex-agent-container-fleet-reconcile.timer", ARMED_MONOTONIC)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.armed == ("scitex-agent-container-fleet-reconcile.timer",)


def test_calendar_timer_with_zero_monotonic_is_armed(tmp_path: Path) -> None:
    # Arrange — a calendar timer's monotonic clock reads `0`, not `infinity`.
    # `0` means "this timer does not use that clock", never "it is dead".
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "dotfiles-drift-check.timer", ARMED_REALTIME)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.state == ARMING_OK


def test_infinity_with_elapsed_is_never_again(tmp_path: Path) -> None:
    # Arrange — THE FAULT, in the exact shape measured on six units. Every
    # other field in this capture says the unit is healthy.
    root = tmp_path / "c"
    root.mkdir()
    _capture(
        root,
        "scitex-agent-container-restart-login-expired-agents.timer",
        DEAD_ELAPSED,
    )
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.never_again == (
        "scitex-agent-container-restart-login-expired-agents.timer",
    )


def test_infinity_while_running_is_not_reported_dead(tmp_path: Path) -> None:
    # Arrange — same `infinity`, and it is CORRECT here: a timer whose
    # service is mid-run has no next elapse until that run ends. Reporting it
    # as dead would make the check cry wolf at the busiest units on the host.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "docs-intake.timer", RUNNING_INFINITY)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.armed == ("docs-intake.timer",)


def test_running_timer_never_counts_as_never_again(tmp_path: Path) -> None:
    # Arrange — the exemption above, asserted from the other side: the
    # offender list must stay EMPTY, not merely "armed as well".
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "docs-intake.timer", RUNNING_INFINITY)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.never_again == ()


def test_a_unit_that_does_not_exist_is_not_called_dead(tmp_path: Path) -> None:
    # Arrange — the recorded reply for a name systemd has never heard of:
    # infinity, no realtime next, SubState=dead, exit 0, no diagnostic. That
    # is the fault's exact signature, so without LoadState the check would
    # accuse a unit that is not there.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "no-such-unit.timer", NOT_FOUND)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.never_again == ()


def test_a_unit_that_does_not_exist_is_unknown(tmp_path: Path) -> None:
    # Arrange — and it lands in UNKNOWN rather than ARMED: sac was not shown a
    # timer, so it has nothing to judge, and "nothing to judge" must not pass.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "no-such-unit.timer", NOT_FOUND)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.unknown == ("no-such-unit.timer",)


def test_unreadable_capture_is_unknown_not_armed(tmp_path: Path) -> None:
    # Arrange — the real bus error, which carries no properties at all.
    # "I could not look" must never be classified as "I looked and it fires".
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "launchpadlib-cache-clean.timer", UNREADABLE)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.unknown == ("launchpadlib-cache-clean.timer",)


def test_unreadable_capture_never_yields_ok(tmp_path: Path) -> None:
    # Arrange — the same capture, asserted on the VERDICT: a host sac could
    # not query is not a host that passed. This is the assertion that stops
    # the check being quietest exactly when it is blindest.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "launchpadlib-cache-clean.timer", UNREADABLE)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.state == ARMING_UNKNOWN


def test_unreadable_capture_exits_non_zero(tmp_path: Path) -> None:
    # Arrange — and it must be non-zero, or a job that could not see anything
    # would be recorded as a clean pass. 2 rather than 1 so a caller can tell
    # "nothing was read" from "a timer is dead".
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "launchpadlib-cache-clean.timer", UNREADABLE)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.exit_code == 2


def test_empty_capture_is_unknown_not_armed(tmp_path: Path) -> None:
    # Arrange — a capture with no bytes at all is the other way a read can
    # fail (a killed subprocess), and it must land in the same place.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "qwen-horizon.timer", "")
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert verdict.unknown == ("qwen-horizon.timer",)


def test_mixed_directory_counts_every_class(one_of_each: Path) -> None:
    # Arrange — all five shapes at once, which is what a real host looks
    # like. The counts are the payload a job or a human reads.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert (
        verdict.armed_count,
        verdict.never_again_count,
        verdict.unknown_count,
    ) == (3, 1, 1)


def test_dead_timer_outranks_an_unreadable_one(one_of_each: Path) -> None:
    # Arrange — the directory holds BOTH a dead timer and an unreadable one.
    # If blindness demoted the verdict to UNKNOWN, the finding a human must
    # act on would be hidden behind the one they cannot.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert verdict.state == ARMING_VIOLATION


def test_dead_timer_exits_one(one_of_each: Path) -> None:
    # Arrange — the gate the incident needed: a non-zero exit the moment any
    # timer will never fire again.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert verdict.exit_code == 1


def test_verdict_names_the_offending_unit(one_of_each: Path) -> None:
    # Arrange — a count alone is not actionable. The verdict must say WHICH
    # unit, and the name must be the one systemd answered with (Id=), not the
    # filename it was asked about.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert verdict.never_again == (
        "scitex-agent-container-restart-login-expired-agents.timer",
    )


def test_population_states_how_many_were_examined(one_of_each: Path) -> None:
    # Arrange — "0 dead" means nothing until the same line says how many were
    # looked at; `0 of 0` and `0 of 22` must not render identically.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert verdict.population().startswith("5 enabled timer(s) examined")


def test_all_armed_directory_is_ok(tmp_path: Path) -> None:
    # Arrange — the healthy case must actually be reachable, or the check is
    # an alarm that never clears.
    root = tmp_path / "c"
    root.mkdir()
    _capture(root, "scitex-agent-container-fleet-reconcile.timer", ARMED_MONOTONIC)
    _capture(root, "dotfiles-drift-check.timer", ARMED_REALTIME)
    _capture(root, "docs-intake.timer", RUNNING_INFINITY)
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert (verdict.state, verdict.exit_code) == (ARMING_OK, 0)


def test_empty_directory_is_ok_with_nothing_examined(tmp_path: Path) -> None:
    # Arrange — an enumeration that RAN and found no enabled timers is a
    # legitimate pass, and is distinguishable from a scan that could not run
    # by the population line rather than by the verdict word.
    root = tmp_path / "c"
    root.mkdir()
    # Act
    verdict = check_timer_liveness(capture_dir=root)
    # Assert
    assert (verdict.state, verdict.population()) == (
        ARMING_OK,
        "0 enabled timer(s) examined, 0 armed, 0 never-again, 0 unreadable",
    )


def test_failed_enumeration_is_unknown_not_ok() -> None:
    # Arrange — zero timers found because nobody could look is not zero
    # timers. This is the `scanned=False` path the live probe takes when
    # `list-unit-files` itself fails.
    readings: tuple[TimerReading, ...] = ()
    # Act
    verdict = verdict_for(readings, scanned=False, detail="Failed to connect to bus")
    # Assert
    assert verdict.state == ARMING_UNKNOWN


def test_failed_enumeration_hint_names_the_reason() -> None:
    # Arrange — a hint that does not say WHY the scan failed is a hint people
    # learn to scroll past.
    # Act
    verdict = verdict_for((), scanned=False, detail="Failed to connect to bus")
    # Assert
    assert "Failed to connect to bus" in verdict.hint()


def test_dead_timer_hint_warns_against_reviving_an_orphan(one_of_each: Path) -> None:
    # Arrange — sac's per-leaf timer lowering is RETIRED and hosts still
    # carry orphan units from it. Re-arming one installs a SECOND scheduler
    # beside the ecosystem supervisor, with independent debounce state, so the
    # remedy must lead with "should this unit exist at all".
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert "ORPHAN" in verdict.hint()


def test_dead_timer_hint_says_start_the_service_first(one_of_each: Path) -> None:
    # Arrange — the obvious repair does not work: restarting the dead TIMER
    # leaves the next elapse in the past (verified on all six units).
    # Starting the SERVICE first, then restarting the timer, is what re-armed
    # them, and a hint that omits that sends the reader in a circle.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert "Start the SERVICE first" in verdict.hint()


def test_reading_states_are_the_three_documented_values(one_of_each: Path) -> None:
    # Arrange — three-valued by construction: nothing may classify into a
    # fourth bucket, and every unit must land in one of them.
    # Act
    verdict = check_timer_liveness(capture_dir=one_of_each)
    # Assert
    assert {r.state for r in verdict.readings} == {
        TIMER_ARMED,
        TIMER_NEVER_AGAIN,
        TIMER_UNKNOWN,
    }


def test_to_dict_carries_the_offenders_for_json(one_of_each: Path) -> None:
    # Arrange — `sac doctor --json` is how a job or a peer reads this, so the
    # offending names have to survive the projection, not only the terminal.
    # Act
    payload = check_timer_liveness(capture_dir=one_of_each).to_dict()
    # Assert
    assert payload["never_again"] == [
        "scitex-agent-container-restart-login-expired-agents.timer"
    ]
