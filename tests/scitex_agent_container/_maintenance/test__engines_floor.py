"""The VERSION FLOOR — the sweep must not strand an agent on an old sac.

Real spec files under ``tmp_path``, planned and applied by the real
functions. No mocks: the fact under test is that a write DOES NOT HAPPEN, and
a mocked writer would only report what the test author believed.

WHY THIS MODULE EXISTS. Reproduced 2026-09-06 by extracting the parent of the
commit that added engines support (``0d61e077``, 2026-09-03) and running THAT
validator over a real fleet spec::

    business/spec.yaml, engines block stripped   -> 0 errors
    the SAME spec + an engines block             -> 1 error:
        "Unknown spec field 'engines'. …"

The zero-error control is the load-bearing half: the block is the whole
difference. Nine of the 119 tracked specs are pinned on hosts that measure as
pre-engines or unmeasured, so an unguarded sweep would make nine specs
unloadable on machines nobody is watching.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._maintenance._engines_apply import (
    apply_engines_migration,
)
from scitex_agent_container._maintenance._engines_floor import (
    HOST_ALIASES,
    REFUSED_HOST_NOT_MEASURED,
    REFUSED_HOST_PREDATES_ENGINES,
    REFUSED_HOST_UNDECLARED,
    REFUSED_HOST_UNREADABLE,
    SUPPORT_NO,
    SUPPORT_UNKNOWN,
    SUPPORT_YES,
    EngineFloor,
    FloorVerdict,
    host_support,
)
from scitex_agent_container._maintenance._engines_migration import (
    STATE_ALREADY,
    STATE_MIGRATED,
    STATE_REFUSED,
    plan_engines_migration,
    select_spec_paths,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

#: Measured pre-engines (3 roots hold the package, none has _engine_types.py).
PREDATES = "spartan"
#: Measured capable.
CAPABLE = "scitex-compute-04"
#: Reachable, but no sac install located — UNKNOWN, therefore refused.
UNMEASURED = "scitex-compute-02"
#: The retired name of the machine recorded as scitex-laptop-01.
RETIRED_ALIAS = "ywata-note-win"


def _write_spec(root: Path, name: str, *, host: str, **overrides) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True)
    spec = {"to_home": "./to_home", "claude": {"model": "opus[1m]"}, "host": host}
    spec.update(overrides)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(explicit_doc(spec), sort_keys=False))
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir()
    return agents


def _plan(root: Path, *, overrides: "tuple[str, ...]" = ()):
    paths, _ = select_spec_paths(root)
    return plan_engines_migration(
        paths, root=root, floor=EngineFloor.with_overrides(overrides)
    )


def _outcome(plan, agent: str):
    return next(o for o in plan.outcomes if o.agent == agent)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The roster itself — measured facts, fail closed
# ---------------------------------------------------------------------------


def test_a_measured_capable_host_reports_supports_engines() -> None:
    # Arrange
    host = CAPABLE
    # Act
    state, _ = host_support(host)
    # Assert
    assert state == SUPPORT_YES


def test_the_host_measured_without_engine_types_reports_predates() -> None:
    # Arrange
    host = PREDATES
    # Act
    state, _ = host_support(host)
    # Assert
    assert state == SUPPORT_NO


def test_a_host_absent_from_the_roster_is_not_measured() -> None:
    # Arrange — fail closed: absence is never read as "probably fine".
    host = "scitex-compute-99"
    # Act
    state, _ = host_support(host)
    # Assert
    assert state == SUPPORT_UNKNOWN


def test_a_measured_host_carries_the_evidence_it_was_measured_by() -> None:
    # Arrange — a row a reader cannot check is a row they must take on trust.
    host = PREDATES
    # Act
    _, record = host_support(host)
    # Assert
    assert "_engine_types.py" in record.evidence


def test_the_retired_alias_resolves_to_the_machine_it_names() -> None:
    # Arrange — `hostname` answers ywata-note-win over BOTH ssh aliases.
    alias = RETIRED_ALIAS
    # Act
    state, _ = host_support(alias)
    # Assert
    assert state == SUPPORT_YES


def test_the_alias_table_names_the_canonical_host() -> None:
    # Arrange
    alias = RETIRED_ALIAS
    # Act
    canonical = HOST_ALIASES[alias]
    # Assert
    assert canonical == "scitex-laptop-01"


def test_a_blocking_verdict_cannot_be_built_without_a_reason() -> None:
    # Arrange — a refusal with no reason is a silent skip wearing a flag.
    construct = FloorVerdict
    # Act
    blocking_with_no_reason = dict(blocks=True)
    # Assert
    with pytest.raises(ValueError):
        construct(**blocking_with_no_reason)


# ---------------------------------------------------------------------------
# The refusal, at PLAN time, in the bucket that already exists
# ---------------------------------------------------------------------------


def test_a_spec_on_a_pre_engines_host_is_refused(root: Path) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "grounded").state == STATE_REFUSED


def test_a_spec_on_a_pre_engines_host_is_not_planned_for_migration(
    root: Path,
) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert plan.migrated == ()


def test_the_refusal_names_the_pre_engines_reason(root: Path) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "grounded").reason == REFUSED_HOST_PREDATES_ENGINES


def test_the_refusal_names_the_host_in_its_detail(root: Path) -> None:
    # Arrange — "which spec, which host, why" is what makes it actionable.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert PREDATES in _outcome(plan, "grounded").detail


def test_the_refusal_names_the_flag_that_lifts_it(root: Path) -> None:
    # Arrange — a floor with no visible exit is a reason to bypass the tool.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert "--host-supports-engines" in _outcome(plan, "grounded").detail


def test_a_floor_refusal_lands_in_the_existing_refused_bucket(root: Path) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert [o.agent for o in plan.refused] == ["grounded"]


def test_a_floor_refusal_leaves_the_plan_safe_to_apply(root: Path) -> None:
    # Arrange — a NAMED refusal is a legitimate outcome, not an unsound plan.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert plan.safe_to_apply is True


def test_a_floor_refusal_leaves_the_migration_incomplete(root: Path) -> None:
    # Arrange — nine refused specs are nine specs a human still has to answer.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root)
    # Assert
    assert plan.is_complete is False


# ---------------------------------------------------------------------------
# Fail closed — the unknown host is the one the assumption strands
# ---------------------------------------------------------------------------


def test_an_unmeasured_host_is_refused_not_assumed_capable(root: Path) -> None:
    # Arrange — compute-02 answered `hostname` and held no sac we could find.
    _write_spec(root, "unknown-ground", host=UNMEASURED)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "unknown-ground").state == STATE_REFUSED


def test_the_unmeasured_refusal_is_a_distinct_reason(root: Path) -> None:
    # Arrange — "predates" is a fact; "not measured" is the absence of one.
    _write_spec(root, "unknown-ground", host=UNMEASURED)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "unknown-ground").reason == REFUSED_HOST_NOT_MEASURED


def test_a_never_seen_host_is_refused(root: Path) -> None:
    # Arrange — extending the fleet must not silently widen what gets written.
    _write_spec(root, "newcomer", host="scitex-compute-77")
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "newcomer").state == STATE_REFUSED


def test_a_spec_naming_no_host_is_refused(root: Path) -> None:
    # Arrange — it could start anywhere, including on a machine that refuses.
    agent_dir = root / "homeless"
    agent_dir.mkdir()
    doc = explicit_doc({"to_home": "./to_home", "claude": {"model": "opus[1m]"}})
    doc["spec"].pop("host", None)
    (agent_dir / "spec.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "homeless").reason == REFUSED_HOST_UNDECLARED


def test_a_definite_negative_outranks_an_unknown() -> None:
    # Arrange — a fact beats the absence of one in the reported reason.
    floor = EngineFloor()
    # Act
    verdict = floor.verdict_for({PREDATES, UNMEASURED})
    # Assert
    assert verdict.reason == REFUSED_HOST_PREDATES_ENGINES


def test_the_unreadable_host_refusal_names_its_own_reason() -> None:
    # Arrange — None means "could not read", which is not "says nothing".
    floor = EngineFloor()
    # Act
    verdict = floor.verdict_for(None)
    # Assert
    assert verdict.reason == REFUSED_HOST_UNREADABLE


# ---------------------------------------------------------------------------
# A capable host is untouched, and the override lifts the floor
# ---------------------------------------------------------------------------


def test_a_spec_on_a_capable_host_still_migrates(root: Path) -> None:
    # Arrange — the floor must not be a wall in front of the 110 good specs.
    _write_spec(root, "fine", host=CAPABLE)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "fine").state == STATE_MIGRATED


def test_the_retired_alias_is_not_refused(root: Path) -> None:
    # Arrange — 14 specs spell scitex-laptop-01 by its retired name.
    _write_spec(root, "old-name", host=RETIRED_ALIAS)
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "old-name").state == STATE_MIGRATED


def test_the_override_lifts_the_floor_for_the_named_host(root: Path) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root, overrides=(PREDATES,))
    # Assert
    assert _outcome(plan, "grounded").state == STATE_MIGRATED


def test_the_override_for_one_host_does_not_lift_another(root: Path) -> None:
    # Arrange — the claim is about the machine named, and only that one.
    _write_spec(root, "grounded", host=PREDATES)
    # Act
    plan = _plan(root, overrides=(UNMEASURED,))
    # Assert
    assert _outcome(plan, "grounded").state == STATE_REFUSED


def test_an_override_typed_as_an_alias_is_recorded_canonically() -> None:
    # Arrange — otherwise an override typed one way silently misses the specs
    # that spell the same machine the other way, in either direction.
    overrides = (RETIRED_ALIAS,)
    # Act
    floor = EngineFloor.with_overrides(overrides)
    # Assert
    assert floor.allowed == frozenset({"scitex-laptop-01"})


def test_an_explicitly_disabled_floor_does_not_refuse(root: Path) -> None:
    # Arrange — EngineFloor.disabled() is the seam the other buckets' tests
    # plan through, and it has to be SAID: see the two tests below for why
    # omitting the argument is no longer the way to say it.
    _write_spec(root, "grounded", host=PREDATES)
    paths, _ = select_spec_paths(root)
    # Act
    plan = plan_engines_migration(paths, root=root, floor=EngineFloor.disabled())
    # Assert
    assert _outcome(plan, "grounded").state == STATE_MIGRATED


def test_planning_without_naming_a_floor_is_a_type_error(root: Path) -> None:
    # Arrange — the documented public planner, called the documented way,
    # planned a spec pinned on a measured pre-engines host as migrated and
    # safe_to_apply. The guard lived in the one caller that remembered to
    # pass a floor; a second entry point would inherit nothing.
    _write_spec(root, "grounded", host=PREDATES)
    paths, _ = select_spec_paths(root)
    # Act
    act = lambda: plan_engines_migration(paths, root=root)  # noqa: E731
    # Assert
    with pytest.raises(TypeError):
        act()


def test_passing_none_as_the_floor_is_refused_by_name(root: Path) -> None:
    # Arrange — None used to MEAN "no floor", so it must not keep meaning it
    # silently now that the argument is required.
    _write_spec(root, "grounded", host=PREDATES)
    paths, _ = select_spec_paths(root)
    # Act
    act = lambda: plan_engines_migration(paths, root=root, floor=None)  # noqa: E731
    # Assert
    with pytest.raises(TypeError, match="EngineFloor.disabled"):
        act()


# ---------------------------------------------------------------------------
# Already-declared on an incapable host is a LIVE incident, not "finished"
# ---------------------------------------------------------------------------


def test_an_already_declared_spec_on_a_pre_engines_host_is_refused(
    root: Path,
) -> None:
    # Arrange — that spec does not load on that host TODAY. Filing it under
    # "already migrated" files a live incident under the bucket meaning done.
    _write_spec(
        root,
        "already-grounded",
        host=PREDATES,
        engines={"default": "claude", "claude": {"harness": "anthropic"}},
    )
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "already-grounded").state == STATE_REFUSED


def test_the_already_declared_refusal_says_it_does_not_load_today(
    root: Path,
) -> None:
    # Arrange
    _write_spec(
        root,
        "already-grounded",
        host=PREDATES,
        engines={"default": "claude", "claude": {"harness": "anthropic"}},
    )
    # Act
    plan = _plan(root)
    # Assert
    assert "does not load on that host today" in _outcome(
        plan, "already-grounded"
    ).detail


def test_an_already_declared_spec_on_a_capable_host_stays_already(
    root: Path,
) -> None:
    # Arrange — the floor must not turn a finished spec into a refusal.
    _write_spec(
        root,
        "done",
        host=CAPABLE,
        engines={"default": "claude", "claude": {"harness": "anthropic"}},
    )
    # Act
    plan = _plan(root)
    # Assert
    assert _outcome(plan, "done").state == STATE_ALREADY


# ---------------------------------------------------------------------------
# THE POINT: the apply writes nothing into the floored spec
# ---------------------------------------------------------------------------


def test_the_apply_leaves_a_floored_spec_byte_identical(
    root: Path, tmp_path: Path
) -> None:
    # Arrange — the whole hazard in one assertion.
    path = _write_spec(root, "grounded", host=PREDATES)
    before = _digest(path)
    plan = _plan(root)
    # Act
    apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert _digest(path) == before


def test_the_apply_still_writes_the_capable_hosts_spec(
    root: Path, tmp_path: Path
) -> None:
    # Arrange — a floor that stops the whole batch would be its own outage.
    _write_spec(root, "grounded", host=PREDATES)
    fine = _write_spec(root, "fine", host=CAPABLE)
    before = _digest(fine)
    plan = _plan(root)
    # Act
    apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert _digest(fine) != before


def test_the_floored_spec_never_reaches_the_written_list(
    root: Path, tmp_path: Path
) -> None:
    # Arrange
    _write_spec(root, "grounded", host=PREDATES)
    _write_spec(root, "fine", host=CAPABLE)
    plan = _plan(root)
    # Act
    result = apply_engines_migration(plan, tmp_path / "archive")
    # Assert
    assert list(result.written) == ["fine"]
