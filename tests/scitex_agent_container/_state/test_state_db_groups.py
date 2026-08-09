"""A group resolution must say WHICH kind of "no group" it found.

WHY THIS EXISTS. ``resolve_group_name`` returns ``""`` for three different
situations, and the host_exec ACL denial built on it said only::

    caller 'alice' resolves to group ''

That is a well-formed answer that cannot distinguish "the true value is
empty" from "I could not determine the value" — and the two need OPPOSITE
operator actions:

    ungrouped      a policy row exists, group_name empty  ->  label the agent
    no_policy_row  no row at all                          ->  check the DB path

The second is the dangerous one. A wrong ``db_path`` presents EVERY agent as
ungrouped, so an operator reading "resolves to group ''" can spend the whole
investigation labelling agents that were already labelled in the database
they meant to be reading.

The collapse originates in ``read_comms_policy``, which documents that it
returns ``DEFAULT_COMMS_POLICY`` for a missing row so the distinction is
"invisible to callers". That is correct for callers wanting defaults, which
is why ``resolve_group`` queries the table directly instead of widening that
helper's contract.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_groups import (
    GroupResolution,
    resolve_group,
    resolve_group_name,
)
from scitex_agent_container._state.state_db_acl_policy import record_comms_policy


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.db"


def test_an_agent_with_a_group_reports_it(db):
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db)
    # Act
    resolution = resolve_group(name="alice", db_path=db)
    # Assert
    assert resolution.group_name == "developer"


def test_an_agent_with_a_group_reports_source_named(db):
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db)
    # Act
    resolution = resolve_group(name="alice", db_path=db)
    # Assert
    assert resolution.source == "named"


def test_a_row_with_an_empty_group_is_ungrouped(db):
    # Arrange — the agent EXISTS and is genuinely in no named group.
    record_comms_policy(name="bob", group_name="", db_path=db)
    # Act
    resolution = resolve_group(name="bob", db_path=db)
    # Assert
    assert resolution.source == "ungrouped"


def test_an_absent_row_is_not_reported_as_ungrouped(db):
    # Arrange — nothing recorded at all.
    # Act
    resolution = resolve_group(name="ghost", db_path=db)
    # Assert — the whole point: this must NOT read as "ungrouped".
    assert resolution.source == "no_policy_row"


def test_an_empty_caller_name_is_its_own_state(db):
    # Arrange
    # Act
    resolution = resolve_group(name="", db_path=db)
    # Assert
    assert resolution.source == "no_caller"


def test_ungrouped_and_absent_are_distinguishable(db):
    # Arrange — the two states the old bare string collapsed.
    record_comms_policy(name="bob", group_name="", db_path=db)
    # Act
    present = resolve_group(name="bob", db_path=db)
    absent = resolve_group(name="ghost", db_path=db)
    # Assert
    assert present.source != absent.source


def test_the_bare_resolver_still_collapses_them(db):
    # Arrange — documents WHY resolve_group exists; if this ever stops being
    # true, the old resolver changed behaviour and callers need review.
    record_comms_policy(name="bob", group_name="", db_path=db)
    # Act
    present = resolve_group_name(name="bob", db_path=db)
    absent = resolve_group_name(name="ghost", db_path=db)
    # Assert
    assert present == absent == ""


def test_absent_row_advice_points_at_the_database(db):
    # Arrange
    resolution = resolve_group(name="ghost", db_path=db)
    # Act
    described = resolution.describe()
    # Assert — an error that only states what broke is half-written.
    assert "db path" in described.lower()


def test_ungrouped_advice_points_at_the_spec_label(db):
    # Arrange
    record_comms_policy(name="bob", group_name="", db_path=db)
    resolution = resolve_group(name="bob", db_path=db)
    # Act
    described = resolution.describe()
    # Assert
    assert "metadata.labels.group" in described


def test_named_description_carries_the_group(db):
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db)
    resolution = resolve_group(name="alice", db_path=db)
    # Act
    described = resolution.describe()
    # Assert
    assert "developer" in described


def test_is_named_is_true_only_for_a_real_group(db):
    # Arrange
    record_comms_policy(name="alice", group_name="developer", db_path=db)
    # Act
    resolution = resolve_group(name="alice", db_path=db)
    # Assert
    assert resolution.is_named is True


def test_is_named_is_false_for_an_absent_row(db):
    # Arrange
    # Act
    resolution = resolve_group(name="ghost", db_path=db)
    # Assert
    assert resolution.is_named is False


def test_a_nonsense_source_is_rejected_where_it_is_built():
    # Arrange
    fields = {"group_name": "", "source": "perhaps"}

    # Act
    def build():
        return GroupResolution(**fields)

    # Assert — the validator fails at construction, not three layers down.
    with pytest.raises(ValueError):
        build()


def test_a_group_name_without_the_named_source_is_rejected():
    # Arrange
    fields = {"group_name": "developer", "source": "ungrouped"}

    # Act
    def build():
        return GroupResolution(**fields)

    # Assert — a resolution must not claim a group while reporting that it
    # could not find one.
    with pytest.raises(ValueError):
        build()


def test_the_named_source_requires_a_group_name():
    # Arrange
    fields = {"group_name": "", "source": "named"}

    # Act
    def build():
        return GroupResolution(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()
