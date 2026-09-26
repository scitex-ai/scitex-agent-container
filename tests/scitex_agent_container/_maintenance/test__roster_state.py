"""A roster that was never searched must not read as a roster with nothing in it.

The regression these guard, measured 2026-08-10 inside an agent container:

    sac agents migrate-layers --json
    {"specs": 0, "writable": 0, "safe_to_apply": true, "exit_code": 0}

against a live fleet of 102 specs, because ``$HOME`` differs host-vs-container
and the resolved root did not exist there.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._maintenance._layers_migration_model import (
    MigrationPlan,
    SpecEdit,
)
from scitex_agent_container._maintenance._roster_state import (
    RosterState,
    inspect_roster,
    inspect_roster_over_roots,
)


@pytest.fixture
def root(tmp_path):
    """An existing, empty registry directory."""
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture
def missing(tmp_path):
    """A registry path that does not exist — the container case."""
    return tmp_path / "nope" / "agents"


def _spec(parent, agent: str):
    d = parent / agent
    d.mkdir(parents=True)
    p = d / "spec.yaml"
    p.write_text("to_home: {}\n")
    return p


@pytest.fixture
def one_spec(root):
    return _spec(root, "alpha")


class TestInspectRoster:
    def test_absent_root_is_classified_absent(self, missing):
        # Arrange
        paths: list = []
        # Act
        state = inspect_roster(missing, paths)
        # Assert
        assert state.state == "absent"

    def test_absent_root_does_not_license_a_claim(self, missing):
        # Arrange
        paths: list = []
        # Act
        state = inspect_roster(missing, paths)
        # Assert
        assert state.is_populated is False

    def test_existing_root_with_no_specs_is_empty(self, root):
        # Arrange
        paths: list = []
        # Act
        state = inspect_roster(root, paths)
        # Assert
        assert state.state == "empty"

    def test_empty_root_does_not_license_a_claim(self, root):
        # Arrange
        paths: list = []
        # Act
        state = inspect_roster(root, paths)
        # Assert
        assert state.is_populated is False

    def test_root_with_specs_is_populated(self, root, one_spec):
        # Arrange
        paths = [one_spec]
        # Act
        state = inspect_roster(root, paths)
        # Assert
        assert state.state == "populated"

    def test_populated_roster_counts_its_specs(self, root, one_spec):
        # Arrange
        paths = [one_spec, _spec(root, "beta")]
        # Act
        state = inspect_roster(root, paths)
        # Assert
        assert state.n_specs == 2

    def test_absent_and_empty_differ_despite_the_same_zero(self, root, missing):
        # Arrange
        no_paths: list = []
        # Act
        absent, empty = (
            inspect_roster(missing, no_paths),
            inspect_roster(root, no_paths),
        )
        # Assert
        assert absent.state != empty.state

    def test_explicit_empty_paths_are_empty_not_absent(self):
        # Arrange
        paths: list = []
        # Act
        state = inspect_roster(None, paths)
        # Assert
        assert state.state == "empty"

    def test_explicit_paths_with_specs_are_populated(self, one_spec):
        # Arrange
        paths = [one_spec]
        # Act
        state = inspect_roster(None, paths)
        # Assert
        assert state.state == "populated"

    def test_unknown_state_is_rejected(self, root):
        # Arrange
        bad = "fine"
        # Act / Assert is a single raises block
        # Assert
        with pytest.raises(ValueError, match="unknown roster state"):
            RosterState(root=root, state=bad)

    def test_absent_describe_names_the_root(self, missing):
        # Arrange
        state = inspect_roster(missing, [])
        # Act
        text = state.describe()
        # Assert
        assert str(missing) in text

    def test_empty_describe_names_the_root(self, root):
        # Arrange
        state = inspect_roster(root, [])
        # Act
        text = state.describe()
        # Assert
        assert str(root) in text

    def test_populated_describe_names_the_root(self, root, one_spec):
        # Arrange
        state = inspect_roster(root, [one_spec])
        # Act
        text = state.describe()
        # Assert
        assert str(root) in text

    def test_absent_describe_denies_it_is_an_empty_fleet(self, missing):
        # Arrange
        state = inspect_roster(missing, [])
        # Act
        text = state.describe()
        # Assert
        assert "NOT an empty fleet" in text

    def test_absent_describe_names_the_lever_that_fixes_it(self, missing):
        # Arrange
        state = inspect_roster(missing, [])
        # Act
        text = state.describe()
        # Assert
        assert "SCITEX_AGENT_CONTAINER_AGENTS_DIR" in text


class TestPlanSafety:
    def test_absent_roster_makes_the_plan_unsafe(self, missing):
        # Arrange
        roster = inspect_roster(missing, [])
        # Act
        plan = MigrationPlan(roster=roster)
        # Assert
        assert plan.safe_to_apply is False

    def test_empty_roster_makes_the_plan_unsafe(self, root):
        # Arrange
        roster = inspect_roster(root, [])
        # Act
        plan = MigrationPlan(roster=roster)
        # Assert
        assert plan.safe_to_apply is False

    def test_populated_roster_with_clean_edits_is_safe(self, root, one_spec):
        # Arrange
        edit = SpecEdit(
            agent="alpha",
            path=one_spec,
            layers=("project-shared",),
            new_text="x",
            lines_added=1,
        )
        # Act
        plan = MigrationPlan(edits=(edit,), roster=inspect_roster(root, [one_spec]))
        # Assert
        assert plan.safe_to_apply is True

    def test_plan_without_a_roster_is_judged_exactly_as_before(self):
        # Arrange
        # A caller that supplied its own population claimed no directory.
        # Act
        plan = MigrationPlan()
        # Assert
        assert plan.safe_to_apply is True

    def test_unsearched_summary_drops_the_would_be_written_count(self, missing):
        # Arrange
        plan = MigrationPlan(roster=inspect_roster(missing, []))
        # Act
        summary = plan.summary()
        # Assert
        assert "would be written" not in summary

    def test_unsearched_summary_names_the_root(self, missing):
        # Arrange
        plan = MigrationPlan(roster=inspect_roster(missing, []))
        # Act
        summary = plan.summary()
        # Assert
        assert str(missing) in summary


class TestSeveralRootsAtOnce:
    """The engines sweep searches EVERY user-scope root, not one.

    The three states keep their meanings, widened by one word: ``absent``
    becomes "NONE of them is a directory". A run where one root of two exists
    but holds nothing DID look somewhere, so it is ``empty`` — a reportable
    fact rather than a discovery failure.
    """

    def test_no_root_existing_is_absent(self, tmp_path):
        # Arrange
        roots = [tmp_path / "nowhere", tmp_path / "also-nowhere"]
        # Act
        roster = inspect_roster_over_roots(roots, [])
        # Assert
        assert roster.state == "absent"

    def test_one_root_existing_and_empty_is_empty_not_absent(
        self, tmp_path, root
    ):
        # Arrange — it DID look somewhere; "nothing here" is a fact.
        roots = [tmp_path / "nowhere", root]
        # Act
        roster = inspect_roster_over_roots(roots, [])
        # Assert
        assert roster.state == "empty"

    def test_specs_found_across_roots_is_populated(self, tmp_path, root):
        # Arrange
        roots = [root, tmp_path / "nowhere"]
        # Act
        roster = inspect_roster_over_roots(roots, [root / "a" / "spec.yaml"])
        # Assert
        assert roster.state == "populated"

    def test_the_populated_description_names_every_searched_root(
        self, tmp_path, root
    ):
        # Arrange — a count naming one root of two is as unreadable as one
        # naming none.
        second = tmp_path / "second"
        second.mkdir()
        roster = inspect_roster_over_roots([root, second], [root / "a" / "spec.yaml"])
        # Act
        described = roster.describe()
        # Assert
        assert str(root) in described and str(second) in described

    def test_an_absent_root_is_not_named_among_the_searched(self, tmp_path, root):
        # Arrange — naming a root that is not there beside one that is invites
        # the reader to conclude the count came from both.
        roster = inspect_roster_over_roots(
            [root, tmp_path / "nowhere"], [root / "a" / "spec.yaml"]
        )
        # Act
        described = roster.describe()
        # Assert
        assert str(tmp_path / "nowhere") not in described

    def test_no_roots_at_all_falls_back_to_the_explicit_paths_shape(self):
        # Arrange — an empty root list claims no directory, so nothing may be
        # called absent.
        roots = []
        # Act
        roster = inspect_roster_over_roots(roots, [])
        # Assert
        assert roster.state == "empty"

    def test_the_representative_root_stays_a_path(self, tmp_path, root):
        # Arrange — widening ``root`` into a joined string would be a type lie
        # for every caller that treats it as a path.
        roots = [root, tmp_path / "nowhere"]
        # Act
        roster = inspect_roster_over_roots(roots, [root / "a" / "spec.yaml"])
        # Assert
        assert roster.root == root
