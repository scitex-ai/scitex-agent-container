#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``rename_comms_grants`` — what it carries, what it refuses, how it unwinds.

The grants half of the agent-rename flow, replacing the two ``NAME_COLUMNS``
pairs that renamed the SQLite table until 2026-08-29.

THREE PROPERTIES, and each is load-bearing rather than tidy:

* IT CARRIES. ``check_send_acl`` asks ``has_grant(sender=<live name>, ...)``,
  so a grant left under the old name is a silent, permanent revocation of a
  permission an operator granted on purpose. A grant left LIVE under the old
  name is the mirror failure: a standing authorisation nobody owns.
* IT REFUSES A REVOKED DESTINATION. ``revoke_send`` hides rather than
  deletes, so a hidden record at the destination is somebody's deliberate
  revoke. Taking it over reinstates a withdrawn grant; skipping it drops the
  one being carried. Both are silent, so the step refuses instead — and the
  tests below construct that state and prove the refusal fires, rather than
  exercising only the happy path.
* ITS UNDO IS KEY-SCOPED. "The same verb with the arguments swapped" is the
  inverse for the policy and directory stores and is WRONG here: the forward
  step hides the sources, so the reversed call meets hidden records at ITS
  destination and refuses. That is asserted directly, so the reason the undo
  is shaped the way it is cannot be lost to a later tidy-up.

Real PostgreSQL via ``pg_schema``, no mocks and no ``monkeypatch``: the
module is driven through its public surface and isolation comes from the
fixture pointing ``SCITEX_STORE_DSN`` at a throwaway schema.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_grants import (
    grant_send,
    has_grant,
    revoke_send,
)
from scitex_agent_container._state.state_db_grants_rename import (
    GrantsRenameError,
    count_grant_rename_rows,
    rename_comms_grants,
    undo_rename_comms_grants,
)
from scitex_agent_container._state.state_db_grants_store import (
    new_grants_store,
    reset_grants_store,
)

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture(autouse=True)
def _drop_cached_handle():
    """Start and end with an empty process-wide handle cache."""
    reset_grants_store()
    yield
    reset_grants_store()


def _row(sender: str, target: str):
    """One stored record, hidden or not, read through a fresh connection."""
    store = new_grants_store()
    try:
        return store.get(
            {"sender_name": sender, "target_name": target}, include_hidden=True
        )
    finally:
        store.close()


def _is_hidden(sender: str, target: str):
    store = new_grants_store()
    try:
        return store.is_hidden({"sender_name": sender, "target_name": target})
    finally:
        store.close()


# ---------------------------------------------------------------------------
# What the step carries
# ---------------------------------------------------------------------------


@pytest.fixture
def carried(pg_schema: str):
    """``OLD`` grants to ``peer``, then ``OLD`` is renamed. Yields the undo."""
    grant_send(sender=OLD, target="peer", note="why-it-was-granted")
    before = float(_row(OLD, "peer").values["created_at"])
    undo = rename_comms_grants(old=OLD, new=NEW)
    yield undo, before


def test_the_new_name_is_authorised_after_the_rename(carried) -> None:
    """The property ``check_send_acl`` actually reads."""
    # Arrange
    _undo, _before = carried
    # Act
    allowed = has_grant(sender=NEW, target="peer")
    # Assert
    assert allowed is True


def test_the_old_name_no_longer_authorises(carried) -> None:
    """A live grant for a name nothing answers to is an ACL row nobody owns."""
    # Arrange
    _undo, _before = carried
    # Act
    allowed = has_grant(sender=OLD, target="peer")
    # Assert
    assert allowed is False


def test_created_at_travels_verbatim(carried) -> None:
    """The permission was given when it was given; a rename is not a re-grant."""
    # Arrange
    _undo, before = carried
    # Act
    after = float(_row(NEW, "peer").values["created_at"])
    # Assert
    assert after == before


def test_the_audit_note_travels_with_the_grant(carried) -> None:
    # Arrange
    _undo, _before = carried
    # Act
    note = _row(NEW, "peer").values["note"]
    # Assert
    assert note == "why-it-was-granted"


def test_the_old_identity_is_hidden_not_hard_deleted(carried) -> None:
    """Nothing is ever hard-deleted, so the history stays auditable."""
    # Arrange
    _undo, _before = carried
    # Act
    hidden = _is_hidden(OLD, "peer")
    # Assert
    assert hidden is True


def test_a_grant_naming_the_agent_as_the_TARGET_moves_too(pg_schema: str) -> None:
    """Both columns were ``NAME_COLUMNS`` pairs; both sides are the identity."""
    # Arrange
    grant_send(sender="peer", target=OLD)
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert has_grant(sender="peer", target=NEW) is True


def test_a_self_grant_moves_on_both_sides_at_once(pg_schema: str) -> None:
    # Arrange
    grant_send(sender=OLD, target=OLD)
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert has_grant(sender=NEW, target=NEW) is True


def test_renaming_an_agent_with_no_grants_is_a_no_op(pg_schema: str) -> None:
    # Arrange
    grant_send(sender="somebody", target="else")
    # Act
    undo = rename_comms_grants(old="never-granted-anything", new=NEW)
    # Assert
    assert undo.total == 0


def test_an_unrelated_grant_is_left_alone(pg_schema: str) -> None:
    """The rename touches the renamed agent's rows and nothing else."""
    # Arrange
    grant_send(sender="stranger", target="somewhere")
    grant_send(sender=OLD, target="peer")
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert has_grant(sender="stranger", target="somewhere") is True


def test_the_dry_run_count_reports_both_sides(pg_schema: str) -> None:
    """The keys the two deleted ``NAME_COLUMNS`` pairs printed under."""
    # Arrange
    grant_send(sender=OLD, target="peer")
    grant_send(sender="peer", target=OLD)
    # Act
    counts = count_grant_rename_rows(old=OLD)
    # Assert
    assert counts == {"comms_grants.sender_name": 1, "comms_grants.target_name": 1}


def test_the_dry_run_count_writes_nothing(pg_schema: str) -> None:
    # Arrange — a preview that mutated would be the worst possible shape for
    # a flag whose whole purpose is "show me what would happen".
    grant_send(sender=OLD, target="peer")
    # Act
    count_grant_rename_rows(old=OLD)
    # Assert
    assert has_grant(sender=OLD, target="peer") is True


# ---------------------------------------------------------------------------
# THE REFUSAL — a revoked grant already standing at the destination
# ---------------------------------------------------------------------------


@pytest.fixture
def refused(pg_schema: str):
    """A REVOKED ``NEW -> peer`` blocks the carry of a live ``OLD -> peer``.

    Constructed through the real verbs: ``grant_send`` then ``revoke_send``
    is exactly how an operator produces a hidden record, and it is the state
    the SQLite ``UPDATE`` could never have distinguished from an empty slot.
    """
    grant_send(sender=NEW, target="peer", note="withdrawn on purpose")
    revoke_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer", note="the live one")
    try:
        rename_comms_grants(old=OLD, new=NEW)
    except GrantsRenameError as exc:
        yield exc
        return
    pytest.fail("rename_comms_grants overwrote a deliberately revoked grant")


def test_a_revoked_destination_refuses_the_rename(pg_schema: str) -> None:
    # Arrange
    grant_send(sender=NEW, target="peer")
    revoke_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer")
    # Act
    # (the rename is the act; it must refuse rather than pick a silent answer)
    # Assert
    with pytest.raises(GrantsRenameError):
        rename_comms_grants(old=OLD, new=NEW)


def test_the_refusal_names_the_blocked_pair(refused) -> None:
    """A refusal must say WHAT it saw, not merely that it refused."""
    # Arrange
    message = str(refused)
    # Act
    named = OLD in message and NEW in message and "peer" in message
    # Assert
    assert named is True


def test_the_refusal_explains_that_the_destination_was_revoked(refused) -> None:
    # Arrange
    message = str(refused)
    # Act
    explained = "REVOKED" in message
    # Assert
    assert explained is True


def test_a_refused_rename_leaves_the_source_grant_live(refused) -> None:
    """Refused BEFORE any write, so there is nothing for the unwind to undo."""
    # Arrange
    _exc = refused
    # Act
    allowed = has_grant(sender=OLD, target="peer")
    # Assert
    assert allowed is True


def test_a_refused_rename_does_not_reinstate_the_revoked_grant(refused) -> None:
    """The failure this refusal exists to prevent, checked directly."""
    # Arrange
    _exc = refused
    # Act
    allowed = has_grant(sender=NEW, target="peer")
    # Assert
    assert allowed is False


def test_a_refused_rename_keeps_the_revoked_record_hidden(refused) -> None:
    # Arrange
    _exc = refused
    # Act
    hidden = _is_hidden(NEW, "peer")
    # Assert
    assert hidden is True


def test_a_live_destination_is_carried_rather_than_refused(pg_schema: str) -> None:
    """A LIVE occupant already grants what the carry was for — not a refusal.

    The asymmetry with the hidden case is the point: a live record is the
    OUTCOME the step wants, while a hidden one is a decision it must not
    overturn.
    """
    # Arrange
    grant_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer")
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert has_grant(sender=NEW, target="peer") is True


def test_a_live_destination_keeps_its_own_created_at(pg_schema: str) -> None:
    """The occupant is left alone; its stamp is not replaced by the source's."""
    # Arrange
    grant_send(sender=NEW, target="peer")
    before = float(_row(NEW, "peer").values["created_at"])
    grant_send(sender=OLD, target="peer")
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert float(_row(NEW, "peer").values["created_at"]) == before


def test_a_live_destination_still_retires_the_source(pg_schema: str) -> None:
    # Arrange
    grant_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer")
    # Act
    rename_comms_grants(old=OLD, new=NEW)
    # Assert
    assert has_grant(sender=OLD, target="peer") is False


# ---------------------------------------------------------------------------
# The undo — key-scoped, and WHY it has to be
# ---------------------------------------------------------------------------


def test_the_undo_restores_the_old_name(carried) -> None:
    # Arrange
    undo, _before = carried
    # Act
    undo_rename_comms_grants(undo)
    # Assert
    assert has_grant(sender=OLD, target="peer") is True


def test_the_undo_retracts_the_new_name(carried) -> None:
    # Arrange
    undo, _before = carried
    # Act
    undo_rename_comms_grants(undo)
    # Assert
    assert has_grant(sender=NEW, target="peer") is False


def test_the_undo_of_an_empty_rename_writes_nothing(pg_schema: str) -> None:
    # Arrange
    undo = rename_comms_grants(old="never-granted-anything", new=NEW)
    grant_send(sender="stranger", target="somewhere")
    # Act
    undo_rename_comms_grants(undo)
    # Assert
    assert has_grant(sender="stranger", target="somewhere") is True


def test_the_undo_leaves_a_pre_existing_destination_grant_alone(
    pg_schema: str,
) -> None:
    """The trap a naive "rename it back" inverse walks into.

    ``NEW -> peer`` was granted BEFORE the rename, so it is not the step's to
    withdraw. Only the identities the forward pass actually created may be
    retracted, which is what key-scoping buys.
    """
    # Arrange
    grant_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer")
    undo = rename_comms_grants(old=OLD, new=NEW)
    # Act
    undo_rename_comms_grants(undo)
    # Assert
    assert has_grant(sender=NEW, target="peer") is True


def test_the_reversed_verb_drags_a_stranger_the_undo_leaves_alone(
    pg_schema: str,
) -> None:
    """The NEGATIVE CONTROL behind the undo's shape.

    ``rename_comms_grants(old=NEW, new=OLD)`` is the inverse the policy and
    directory stores use, and it looks symmetric here too. It is not: it
    carries EVERY grant naming ``NEW``, including one that was granted before
    the rename and has nothing to do with it. This asserts the wrong
    behaviour happens, so the reason the undo is key-scoped cannot be lost to
    a later "make these consistent" edit.
    """
    # Arrange
    grant_send(sender=NEW, target="stranger")
    grant_send(sender=OLD, target="peer")
    rename_comms_grants(old=OLD, new=NEW)
    # Act
    rename_comms_grants(old=NEW, new=OLD)
    # Assert
    assert has_grant(sender=OLD, target="stranger") is True


def test_the_key_scoped_undo_leaves_that_stranger_where_it_was(
    pg_schema: str,
) -> None:
    """The same world, unwound properly. Only what was touched moves back."""
    # Arrange
    grant_send(sender=NEW, target="stranger")
    grant_send(sender=OLD, target="peer")
    undo = rename_comms_grants(old=OLD, new=NEW)
    # Act
    undo_rename_comms_grants(undo)
    # Assert
    assert has_grant(sender=OLD, target="stranger") is False


# ---------------------------------------------------------------------------
# Renaming BACK — the destination this step retired itself
# ---------------------------------------------------------------------------


def test_a_rename_can_be_reversed_as_a_fresh_rename(carried) -> None:
    """A rename must not be a one-way door for an agent that holds a grant.

    The forward pass HID ``OLD -> peer``, so renaming back meets a hidden
    record at its destination. Refusing there would be the same mistake
    ``rename_comms_node`` names for itself — it would make the documented
    inverse impossible. The stamp is what tells the two cases apart.
    """
    # Arrange
    _undo, _before = carried
    # Act
    rename_comms_grants(old=NEW, new=OLD)
    # Assert
    assert has_grant(sender=OLD, target="peer") is True


def test_the_reversal_retires_the_new_name_again(carried) -> None:
    # Arrange
    _undo, _before = carried
    # Act
    rename_comms_grants(old=NEW, new=OLD)
    # Assert
    assert has_grant(sender=NEW, target="peer") is False


def test_the_round_trip_keeps_the_original_created_at(carried) -> None:
    """Revived, not rewritten — so the stamp is still the original one."""
    # Arrange
    _undo, before = carried
    # Act
    rename_comms_grants(old=NEW, new=OLD)
    # Assert
    assert float(_row(OLD, "peer").values["created_at"]) == before


def test_a_revoked_destination_with_its_own_stamp_still_refuses(
    pg_schema: str,
) -> None:
    """The discriminator is the STAMP, not "was it hidden by a rename".

    Same shape as the round trip — a hidden record sitting at the
    destination — but this one was granted at its own moment and revoked on
    purpose, so it must still block. Without this the exception above would
    be a hole rather than a distinction.
    """
    # Arrange
    grant_send(sender=NEW, target="peer")
    revoke_send(sender=NEW, target="peer")
    grant_send(sender=OLD, target="peer")
    # Act
    # Assert
    with pytest.raises(GrantsRenameError):
        rename_comms_grants(old=OLD, new=NEW)

# EOF
