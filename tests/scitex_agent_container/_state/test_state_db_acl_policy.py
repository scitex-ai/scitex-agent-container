#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""node_comms_policy on PostgreSQL — and gates that actually flip.

This is the table ``check_send_acl`` and ``check_spawn`` read, so the tests
that matter are the FLIPS: not "a policy can be written" but "after the spec
says deny, the thing the gate calls says NO — and after it says allow again,
the same call says YES". A migration that stored policies perfectly and
forgot to flip would read as green on every round-trip assertion and be a
security regression in both directions at once.

FOUR PROPERTIES THAT ARE EASY TO LOSE HERE, each with its own test below:

  * A DENY DENIES, AND ITS REMOVAL UN-DENIES. ``Store.put`` is a PARTIAL
    update — absent fields are left alone — so a ``record_comms_policy``
    that wrote only the caller's non-default arguments would let a previous
    ``inbound_siblings="deny"`` survive the spec edit that removed it.
    ``test_re_publishing_without_the_deny_flips_it_back_to_allow`` writes the
    deny, re-publishes with defaults, and asserts the deny is GONE. A partial
    write fails that test; the full-record write passes it.
  * DROPPING A GROUP REVOKES IT. Same hazard, higher stakes: ``group_names``
    is what ``is_developer`` reads, so a stale value is a standing privilege.
  * RETIREMENT DENIES WITHOUT FORGETTING. ``retire_comms_policy`` does not
    DELETE — the store's only removal is ``hide``. If ``read_comms_policy``
    read hidden records it would keep enforcing a withdrawn policy; if
    hiding lost the record, "never registered" and "registered then retired"
    would become indistinguishable, and for an ACL that is the whole audit.
  * A RETIRED POLICY RE-OPENS THE GATE, and that is worth an explicit test
    rather than a footnote: a missing record reads as the all-allow
    defaults, so retiring a ``may_spawn=false`` policy RESTORES the spawn.
    Silence is permission here, which is exactly why the store must raise
    rather than return empty when PostgreSQL is unreachable.

WHICH TESTS NEED POSTGRESQL, AND WHICH DELIBERATELY DO NOT. The validators
run BEFORE any store is opened, so the rejection tests exercise real
behaviour on a host with no database and stay green there. That is a
property, not a convenience: a bad ACL value must be refused identically
whether or not the store happens to be reachable. Everything that reads or
writes takes ``pg_schema``, the shared opt-in fixture, which skips where no
cluster exists and FAILS where a configured one is broken.

``sender_target_relationship`` used to be tested here. It reads ``lineage``,
which is still SQLite, so it moved to
``test_state_db_lineage_rel.py`` alongside the module it moved to — rather
than staying in a file whose name now promises PostgreSQL.

NO MONKEYPATCH (PA-306 §3): the module is exercised through its real public
surface, and isolation comes from the fixture pointing SCITEX_STORE_DSN at a
throwaway schema.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_acl_policy import (
    _open,
    comms_policy_row_exists,
    rename_comms_policy,
    retire_comms_policy,
)
from scitex_agent_container._state.state_db_nodes import (
    apply_may_spawn_gate,
    read_comms_policy,
    record_comms_policy,
)

# ---------------------------------------------------------------------------
# the per-spec deny flips, in BOTH directions
# ---------------------------------------------------------------------------


def test_a_recorded_deny_is_in_force(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="cap-a", inbound_siblings="deny")
    # Act
    policy = read_comms_policy(name="cap-a")
    # Assert
    assert policy["inbound_siblings"] == "deny"


def test_re_publishing_without_the_deny_flips_it_back_to_allow(
    pg_schema: str,
) -> None:
    # Arrange — the spec-edit path: agent_start re-publishes on every start,
    # and the second call names no deny at all.
    record_comms_policy(name="cap-a", inbound_siblings="deny")
    # Act
    record_comms_policy(name="cap-a")
    # Assert — a partial write would leave "deny" standing here.
    assert read_comms_policy(name="cap-a")["inbound_siblings"] == "allow"


def test_dropping_a_group_from_the_spec_revokes_it(pg_schema: str) -> None:
    # Arrange — an agent whose spec listed developer, then stopped.
    record_comms_policy(name="cap-a", group_names=["generalist", "developer"])
    # Act
    record_comms_policy(name="cap-a", group_names=["generalist"])
    # Assert — a stale group_names is a standing privilege.
    assert "developer" not in read_comms_policy(name="cap-a")["group_names"]


# ---------------------------------------------------------------------------
# the spawn gate — the one that turns a policy value into a decision
# ---------------------------------------------------------------------------


def test_may_spawn_false_denies_a_spawn_the_global_policy_allowed(
    pg_schema: str,
) -> None:
    # Arrange
    record_comms_policy(name="cap-a", may_spawn=False)
    # Act
    allowed, _reason = apply_may_spawn_gate(caller="cap-a", base=(True, None))
    # Assert
    assert allowed is False


def test_re_publishing_may_spawn_true_restores_the_spawn(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="cap-a", may_spawn=False)
    record_comms_policy(name="cap-a", may_spawn=True)
    # Act
    allowed, _reason = apply_may_spawn_gate(caller="cap-a", base=(True, None))
    # Assert
    assert allowed is True


def test_may_spawn_never_rescues_an_already_denied_spawn(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="cap-a", may_spawn=True)
    # Act
    allowed, _reason = apply_may_spawn_gate(caller="cap-a", base=(False, "no"))
    # Assert
    assert allowed is False


def test_may_spawn_round_trips_as_a_real_bool(pg_schema: str) -> None:
    # Arrange — the SQLite column was 0/1; the store field is BOOL.
    record_comms_policy(name="cap-a", may_spawn=False)
    # Act
    value = read_comms_policy(name="cap-a")["may_spawn"]
    # Assert
    assert value is False


# ---------------------------------------------------------------------------
# retirement — hide, never delete
# ---------------------------------------------------------------------------


def test_retire_reports_true_when_a_live_policy_was_withdrawn(
    pg_schema: str,
) -> None:
    # Arrange
    record_comms_policy(name="cap-a", may_spawn=False)
    # Act
    withdrawn = retire_comms_policy(name="cap-a")
    # Assert
    assert withdrawn is True


def test_retire_reports_false_the_second_time(pg_schema: str) -> None:
    # Arrange — the hidden record still occupies the identity, so a naive
    # "does the record exist" check would answer True and lie.
    record_comms_policy(name="cap-a")
    retire_comms_policy(name="cap-a")
    # Act
    again = retire_comms_policy(name="cap-a")
    # Assert
    assert again is False


def test_a_retired_policy_is_absent_from_the_diagnostic(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="cap-a")
    retire_comms_policy(name="cap-a")
    # Act
    registered = comms_policy_row_exists(name="cap-a")
    # Assert
    assert registered is False


def test_retiring_a_may_spawn_deny_reopens_the_spawn(pg_schema: str) -> None:
    # Arrange — the surprising direction, asserted on purpose: a missing
    # record reads as the all-allow defaults, so withdrawing a policy grants
    # rather than revokes. Silence is permission in this table.
    record_comms_policy(name="cap-a", may_spawn=False)
    retire_comms_policy(name="cap-a")
    # Act
    allowed, _reason = apply_may_spawn_gate(caller="cap-a", base=(True, None))
    # Assert
    assert allowed is True


def test_a_retired_policy_is_still_on_record(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="cap-a", group_names=["developer"])
    retire_comms_policy(name="cap-a")
    # Act
    store = _open()
    try:
        row = store.get({"name": "cap-a"}, include_hidden=True)
    finally:
        store.close()
    # Assert — DELETE could not answer "what groups did it hold?".
    assert row is not None and row.values["group_names"] == "developer"


def test_re_recording_a_retired_policy_restores_it(pg_schema: str) -> None:
    # Arrange — impossible to express under DELETE; the record is hidden and
    # an insert would collide with the identity it still occupies.
    record_comms_policy(name="cap-a", inbound_siblings="deny")
    retire_comms_policy(name="cap-a")
    # Act
    record_comms_policy(name="cap-a", inbound_siblings="deny")
    # Assert
    assert read_comms_policy(name="cap-a")["inbound_siblings"] == "deny"


# ---------------------------------------------------------------------------
# rename — an IDENTITY change, so a copy + retire rather than an update
# ---------------------------------------------------------------------------


def test_rename_carries_the_policy_to_the_new_name(pg_schema: str) -> None:
    # Arrange — miss this and the ACL gate has no policy for the live name.
    record_comms_policy(name="scitex-todo", group_names=["developer"])
    # Act
    rename_comms_policy(old="scitex-todo", new="scitex-cards")
    # Assert
    assert "developer" in read_comms_policy(name="scitex-cards")["group_names"]


def test_rename_retires_the_old_name(pg_schema: str) -> None:
    # Arrange — a live policy under a name that no longer exists is a
    # standing authorisation nobody owns.
    record_comms_policy(name="scitex-todo", group_names=["developer"])
    # Act
    rename_comms_policy(old="scitex-todo", new="scitex-cards")
    # Assert
    assert comms_policy_row_exists(name="scitex-todo") is False


def test_rename_keeps_the_old_name_on_record(pg_schema: str) -> None:
    # Arrange
    record_comms_policy(name="scitex-todo", group_names=["developer"])
    rename_comms_policy(old="scitex-todo", new="scitex-cards")
    # Act
    store = _open()
    try:
        row = store.get({"name": "scitex-todo"}, include_hidden=True)
    finally:
        store.close()
    # Assert
    assert row is not None and row.hidden is True


def test_rename_reports_false_when_nothing_is_live(pg_schema: str) -> None:
    # Arrange — the re-run-after-a-partial-rename case: it must not clobber
    # whatever already sits under the new name.
    record_comms_policy(name="scitex-cards", group_names=["developer"])
    # Act
    moved = rename_comms_policy(old="scitex-todo", new="scitex-cards")
    # Assert
    assert moved is False


# ---------------------------------------------------------------------------
# the diagnostic distinction the 2026-08-09 escalation needed
# ---------------------------------------------------------------------------


def test_row_exists_is_false_for_a_name_this_store_never_saw(
    pg_schema: str,
) -> None:
    # Arrange
    record_comms_policy(name="cap-a")
    # Act
    registered = comms_policy_row_exists(name="never-recorded")
    # Assert
    assert registered is False


def test_row_exists_is_true_for_a_registered_but_ungrouped_agent(
    pg_schema: str,
) -> None:
    # Arrange — the OTHER cause of an empty group set, and the one the
    # denial message used to assert without checking.
    record_comms_policy(name="cap-a")
    # Act
    registered = comms_policy_row_exists(name="cap-a")
    # Assert
    assert registered is True


def test_read_returns_the_legacy_defaults_when_no_record_exists(
    pg_schema: str,
) -> None:
    # Arrange — no record_comms_policy call for this name.
    record_comms_policy(name="someone-else")
    # Act
    policy = read_comms_policy(name="never-recorded")
    # Assert
    assert policy["outbound_siblings"] == "allow"


def test_the_primary_group_is_folded_into_the_authority_set(
    pg_schema: str,
) -> None:
    # Arrange — the two projections are written together so the stored set
    # is never a strict subset of what the mesh already resolves.
    record_comms_policy(name="cap-a", group_name="infra", group_names=["developer"])
    # Act
    groups = read_comms_policy(name="cap-a")["group_names"]
    # Assert
    assert set(groups) == {"developer", "infra"}


# ---------------------------------------------------------------------------
# refusals — these run BEFORE any store is opened, so they need no database
# ---------------------------------------------------------------------------


def test_an_empty_name_is_refused() -> None:
    # Arrange
    refused = None
    # Act
    try:
        record_comms_policy(name="")
    except ValueError as exc:
        refused = exc
    # Assert
    assert refused is not None


def test_an_unknown_outbound_siblings_value_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", outbound_siblings="maybe")


def test_an_unknown_outbound_parent_value_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", outbound_parent="maybe")


def test_an_unknown_inbound_siblings_value_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", inbound_siblings="maybe")


def test_an_unknown_inbound_parent_value_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", inbound_parent="maybe")


def test_an_unknown_lineage_group_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", lineage_group="cluster")


def test_a_non_bool_may_spawn_is_refused() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", may_spawn="nope")


def test_a_group_name_containing_a_comma_is_refused() -> None:
    # Arrange — the encoding is comma-separated, so accepting one would
    # silently split a single group into two.
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", group_names=["dev,ops"])


def test_a_bare_string_group_names_is_refused() -> None:
    # Arrange — without this it would splat into its characters and store
    # each letter as a group name.
    # Act
    # Assert
    with pytest.raises(ValueError):
        record_comms_policy(name="cap-a", group_names="developer")


def test_an_empty_name_reads_the_defaults_without_touching_the_store() -> None:
    # Arrange
    # Act
    policy = read_comms_policy(name="")
    # Assert
    assert policy["may_spawn"] is True


def test_row_exists_is_false_for_an_empty_name_without_touching_the_store() -> None:
    # Arrange
    # Act
    registered = comms_policy_row_exists(name="")
    # Assert
    assert registered is False


def test_renaming_a_name_onto_itself_is_refused_without_touching_the_store() -> None:
    # Arrange
    # Act
    moved = rename_comms_policy(old="cap-a", new="cap-a")
    # Assert
    assert moved is False
