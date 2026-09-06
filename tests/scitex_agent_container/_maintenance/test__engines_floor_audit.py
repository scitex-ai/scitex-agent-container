"""The floor has to carry its EVIDENCE on the approving side too.

Real spec files under ``tmp_path``, planned by the real functions. Two facts
live here and they are opposite halves of the same asymmetry:

**THE APPROVALS SAID NOTHING.** Every refusal prints its measurement and its
date; the 100 writes printed neither. ``HOST_SUPPORT`` is a static table with
no expiry and the design deliberately does not probe, so the one way it fails
OPEN is a row going stale — a host rebuilt, rolled back, or reinstalled onto
an older sac after ``measured_on``. In that case the sweep writes those specs
and the report is byte-for-byte indistinguishable from a correct run.

**THE REFUSALS SAID TOO MUCH.** The "already declares engines, so it does not
load on that host today" sentence was appended to EVERY floor refusal of an
already-declaring spec, including the two whose own preceding sentence says
nobody knows anything about that host:

    scitex-compute-77: not-measured — absent from the measured roster. […]
    This spec ALREADY declares spec.engines, so it does not load on that host
    today […]

Nobody measured that host; nothing established any load failure on it. Under
the no-host reason there is no "that host" to refer to at all. Only
``REFUSED_HOST_PREDATES_ENGINES`` measured a rejection, so only it earns the
sentence.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_floor import (
    SUPPORT_NO,
    SUPPORT_UNKNOWN,
    SUPPORT_YES,
    EngineFloor,
)
from scitex_agent_container._maintenance._engines_floor_audit import floor_audit
from scitex_agent_container._maintenance._engines_migration import (
    plan_engines_migration,
    select_spec_paths,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

CAPABLE = "scitex-compute-04"
PREDATES = "spartan"
UNMEASURED = "scitex-compute-02"
NEVER_SEEN = "scitex-compute-77"

#: The sentence that asserts a load failure. Only the measured-negative
#: refusal is entitled to it.
_ASSERTS_A_LOAD_FAILURE = "does not load on that host today"


def _write_spec(root: Path, name: str, *, host: "str | None" = CAPABLE) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    spec = {"to_home": "./to_home", "claude": {"model": "opus[1m]"}}
    if host is not None:
        spec["host"] = host
    doc = explicit_doc(spec)
    if host is None:
        doc["spec"].pop("host", None)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir()
    return agents


def _already_declaring(root: Path, name: str, *, host: "str | None") -> Path:
    """A spec that ALREADY carries an engines block, on ``host``.

    Written by the real editor with the floor explicitly disabled, so the
    block is exactly what the sweep would have produced.
    """
    path = _write_spec(root, name, host=host)
    plan = plan_engines_migration([path], floor=EngineFloor.disabled())
    path.write_text(plan.migrated[0].new_text)
    return path


def _detail(root: Path, agent: str) -> str:
    paths, _ = select_spec_paths(root)
    plan = plan_engines_migration(paths, root=root, floor=EngineFloor())
    return next(o for o in plan.outcomes if o.agent == agent).detail


def _audit(root: Path, *, overrides: "tuple[str, ...]" = ()):
    floor = EngineFloor.with_overrides(overrides)
    paths, _ = select_spec_paths(root)
    plan = plan_engines_migration(paths, root=root, floor=floor)
    return floor_audit(
        floor, [o.hosts if o.hosts is None else set(o.hosts) for o in plan.outcomes]
    )


def _row(audit, host: str):
    return next(r for r in audit.hosts if r.host == host)


# ---------------------------------------------------------------------------
# The refusal must not assert what its own reason says nobody knows
# ---------------------------------------------------------------------------


def test_a_never_measured_host_refusal_asserts_no_load_failure(root: Path) -> None:
    # Arrange — "absent from the measured roster" and "it does not load there
    # today" cannot both be true of one host in one paragraph.
    _already_declaring(root, "newcomer", host=NEVER_SEEN)
    # Act
    detail = _detail(root, "newcomer")
    # Assert
    assert _ASSERTS_A_LOAD_FAILURE not in detail


def test_an_unmeasured_host_refusal_asserts_no_load_failure(root: Path) -> None:
    # Arrange — compute-02 answered `hostname` and held no sac we could find;
    # that is not knowing, not a measured rejection.
    _already_declaring(root, "unknown-ground", host=UNMEASURED)
    # Act
    detail = _detail(root, "unknown-ground")
    # Assert
    assert _ASSERTS_A_LOAD_FAILURE not in detail


def test_a_host_less_refusal_asserts_no_load_failure(root: Path) -> None:
    # Arrange — there is no "that host" for the sentence to refer to, and the
    # preceding reason does not even end in a full stop, so the two ran on
    # into "…would reject the block This spec ALREADY declares…".
    _already_declaring(root, "homeless", host=None)
    # Act
    detail = _detail(root, "homeless")
    # Assert
    assert _ASSERTS_A_LOAD_FAILURE not in detail


def test_a_measured_pre_engines_refusal_still_asserts_the_load_failure(
    root: Path,
) -> None:
    # Arrange — the positive control, and the whole point of the sentence: on
    # THIS host a rejection was measured, so the spec really does not load
    # today and filing it under "already migrated" would file a live incident.
    _already_declaring(root, "grounded", host=PREDATES)
    # Act
    detail = _detail(root, "grounded")
    # Assert
    assert _ASSERTS_A_LOAD_FAILURE in detail


# ---------------------------------------------------------------------------
# The approving side carries its evidence
# ---------------------------------------------------------------------------


def test_the_audit_names_the_host_the_writes_were_judged_by(root: Path) -> None:
    # Arrange
    _write_spec(root, "alpha", host=CAPABLE)
    # Act
    audit = _audit(root)
    # Assert
    assert [r.host for r in audit.hosts] == [CAPABLE]


def test_an_approving_row_carries_its_measurement_date(root: Path) -> None:
    # Arrange — a static table with no expiry fails open by going stale, and
    # the date is what lets a reader ask whether it is still true.
    _write_spec(root, "alpha", host=CAPABLE)
    # Act
    audit = _audit(root)
    # Assert
    assert _row(audit, CAPABLE).measured_on == "2026-09-06"


def test_an_approving_row_carries_the_evidence_behind_it(root: Path) -> None:
    # Arrange
    _write_spec(root, "alpha", host=CAPABLE)
    # Act
    audit = _audit(root)
    # Assert
    assert "config/_engine_types.py" in _row(audit, CAPABLE).evidence


def test_the_audit_counts_the_specs_each_verdict_covers(root: Path) -> None:
    # Arrange — two approved, one refused: the count is per VERDICT, which is
    # what "how much of this run rests on that row" means.
    _write_spec(root, "alpha", host=CAPABLE)
    _write_spec(root, "beta", host=CAPABLE)
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    audit = _audit(root)
    # Assert
    assert audit.counts == {SUPPORT_NO: 1, SUPPORT_YES: 2}


def test_the_audit_reports_the_oldest_roster_date_consulted(root: Path) -> None:
    # Arrange — a run's roster is only as current as its oldest row.
    _write_spec(root, "alpha", host=CAPABLE)
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    audit = _audit(root)
    # Assert
    assert audit.measured_on == ("2026-09-06",)


def test_a_spec_naming_no_host_is_counted_apart(root: Path) -> None:
    # Arrange — "it says nothing" is not a host row.
    _write_spec(root, "homeless", host=None)
    # Act
    audit = _audit(root)
    # Assert
    assert audit.specs_with_no_declared_host == 1


def test_a_spec_whose_hosts_could_not_be_read_is_counted_apart(root: Path) -> None:
    # Arrange — "I could not read it" and "it says nothing" want different
    # fixes, so the audit keeps them apart exactly as the floor does.
    broken = _write_spec(root, "broken", host=CAPABLE)
    broken.chmod(0o000)
    # Act
    try:
        audit = _audit(root)
    finally:
        broken.chmod(0o644)
    # Assert
    assert audit.specs_with_an_unreadable_host == 1


# ---------------------------------------------------------------------------
# An override of a MEASURED NO is not an override of an unknown
# ---------------------------------------------------------------------------


def test_an_override_of_a_measured_negative_is_flagged_as_contradicting(
    root: Path,
) -> None:
    # Arrange — the roster records spartan as predates-engines with a positive
    # control. Lifting it writes blocks that, by that same measurement, then
    # fail the host's validator and stop those agents starting.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    audit = _audit(root, overrides=(PREDATES,))
    # Assert
    assert _row(audit, PREDATES).contradicts_a_measurement is True


def test_an_override_of_an_unknown_is_not_flagged_as_contradicting(
    root: Path,
) -> None:
    # Arrange — nobody measured compute-02, so there is no measurement to
    # contradict. Categorically a different claim, and it must not be
    # reported as the loud one.
    _write_spec(root, "unknown-ground", host=UNMEASURED)
    # Act
    audit = _audit(root, overrides=(UNMEASURED,))
    # Assert
    assert _row(audit, UNMEASURED).contradicts_a_measurement is False


def test_the_overridden_row_still_carries_the_measurement_it_lifts(
    root: Path,
) -> None:
    # Arrange — an override has to RESTATE what it is arguing with, or the
    # reader never sees the fact being overridden.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    audit = _audit(root, overrides=(PREDATES,))
    # Assert
    assert "NONE has" in _row(audit, PREDATES).evidence


def test_an_override_naming_a_host_no_spec_mentions_is_still_reported(
    root: Path,
) -> None:
    # Arrange — a lift typed for a host that is not in this batch did nothing,
    # and reads as though it did.
    _write_spec(root, "alpha", host=CAPABLE)
    # Act
    audit = _audit(root, overrides=(PREDATES,))
    # Assert
    assert _row(audit, PREDATES).specs == 0


def test_a_disabled_floor_reports_itself_as_inactive(root: Path) -> None:
    # Arrange — the report must not present a floor's basis for a run that
    # had no floor.
    _write_spec(root, "alpha", host=CAPABLE)
    # Act
    audit = floor_audit(EngineFloor.disabled(), [{CAPABLE}])
    # Assert
    assert audit.active is False


def test_an_unmeasured_row_carries_no_invented_date(root: Path) -> None:
    # Arrange — an unmeasured host has no measurement to date, and a blank is
    # the honest value.
    _write_spec(root, "newcomer", host=NEVER_SEEN)
    # Act
    audit = _audit(root)
    # Assert
    assert (_row(audit, NEVER_SEEN).support, _row(audit, NEVER_SEEN).measured_on) == (
        SUPPORT_UNKNOWN,
        "",
    )
