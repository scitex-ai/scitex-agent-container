#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents rename`` must carry the agent's comms grants.

THE DEFECT THESE PIN. ``comms_grants`` moved to the PostgreSQL store on
2026-08-28, but its two pairs were left in
``_lifecycle._rename_db.NAME_COLUMNS``. ``rename_rows`` SKIPS a table absent
from ``sqlite_master``, so the rename reported success while every grant kept
the OLD name. Measured before the fix, on a state.db built as the package
builds one::

    count_rows('old-agent') -> {}

``test_the_dry_run_reports_the_grants_it_will_carry`` is the direct pin for
that ``{}``: a dry run that reports nothing is what let this ship.

Real store, throwaway schema via ``pg_schema`` — no mocks, no monkeypatch.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state.state_db_grants import (
    grant_send,
    has_grant,
    revoke_send,
)
from scitex_agent_container._state.state_db_grants_rename import (
    count_grant_rename_rows,
    rename_grant_rows,
    undo_rename_grant_rows,
)

OLD = "agent-before"
NEW = "agent-after"
PEER = "lead"


@pytest.fixture(autouse=True)
def _grants_store(pg_schema: str):
    """A throwaway grants store for every test here.

    Autouse because every verb in this file opens the store; without it a new
    test would silently resolve whatever store the process points at, which
    for this table is the live fleet's authorisations.
    """
    yield


class TestTheAgentAsSender:
    def test_the_grant_arrives_under_the_new_name(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=NEW, target=PEER) is True

    def test_the_old_name_stops_authorising(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        # a live grant naming an agent that no longer exists is an
        # authorisation nobody owns.
        assert has_grant(sender=OLD, target=PEER) is False


class TestTheAgentAsTarget:
    def test_a_grant_pointing_AT_the_agent_follows_it(self):
        # Arrange
        grant_send(sender=PEER, target=OLD, note="pin")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=PEER, target=NEW) is True

    def test_the_old_target_stops_authorising(self):
        # Arrange
        grant_send(sender=PEER, target=OLD, note="pin")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=PEER, target=OLD) is False


class TestBothSidesAtOnce:
    def test_a_self_grant_moves_wholesale(self):
        # Arrange -- the agent appears on BOTH sides of one record.
        grant_send(sender=OLD, target=OLD, note="self")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=NEW, target=NEW) is True


class TestAnUnrelatedGrantIsUntouched:
    def test_a_similar_name_is_not_rewritten(self):
        # Arrange -- whole-value equality, never substring.
        grant_send(sender=f"{OLD}-archive", target=PEER, note="bystander")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=f"{OLD}-archive", target=PEER) is True


class TestALiveOccupantIsFoldedRatherThanRefused:
    def test_the_authorisation_survives(self):
        # Arrange -- new -> PEER is ALREADY granted.
        grant_send(sender=NEW, target=PEER, note="incumbent")
        grant_send(sender=OLD, target=PEER, note="incoming")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        # a grant is not exclusive, so folding one into an existing one
        # changes nothing a caller can observe.
        assert has_grant(sender=NEW, target=PEER) is True

    def test_the_old_grant_is_still_withdrawn(self):
        # Arrange
        grant_send(sender=NEW, target=PEER, note="incumbent")
        grant_send(sender=OLD, target=PEER, note="incoming")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=OLD, target=PEER) is False


class TestARevokedOccupantIsRestored:
    def test_a_live_grant_is_not_downgraded_into_a_revoked_one(self):
        # Arrange -- new -> PEER was granted and then REVOKED, so the identity
        # is occupied by a hidden row; old -> PEER is LIVE and must survive.
        grant_send(sender=NEW, target=PEER, note="was revoked")
        revoke_send(sender=NEW, target=PEER)
        grant_send(sender=OLD, target=PEER, note="live")
        # Act
        rename_grant_rows(old=OLD, new=NEW)
        # Assert
        assert has_grant(sender=NEW, target=PEER) is True


class TestTheDryRunReportsWhatItWillDo:
    def test_the_dry_run_reports_the_grants_it_will_carry(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        # Act
        counts = count_grant_rename_rows(old=OLD)
        # Assert
        # THE REGRESSION PIN: this returned {} while the rename looked for
        # comms_grants in sqlite_master, and an empty dry run is what let a
        # silent no-op ship.
        assert counts == {"comms_grants.sender_name": 1}

    def test_the_dry_run_writes_nothing(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        # Act
        count_grant_rename_rows(old=OLD)
        # Assert
        assert has_grant(sender=OLD, target=PEER) is True


class TestTheUndo:
    def test_the_old_grant_comes_back(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        undo = rename_grant_rows(old=OLD, new=NEW)
        # Act
        undo_rename_grant_rows(undo)
        # Assert
        assert has_grant(sender=OLD, target=PEER) is True

    def test_the_new_grant_goes_away_again(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        undo = rename_grant_rows(old=OLD, new=NEW)
        # Act
        undo_rename_grant_rows(undo)
        # Assert
        assert has_grant(sender=NEW, target=PEER) is False

    def test_a_grant_that_already_named_new_is_left_alone(self):
        # Arrange -- the reason the undo is KEY-SCOPED rather than "run the
        # rename backwards": this grant legitimately named NEW beforehand.
        grant_send(sender=NEW, target=PEER, note="predates the rename")
        grant_send(sender=OLD, target=PEER, note="incoming")
        undo = rename_grant_rows(old=OLD, new=NEW)
        # Act
        undo_rename_grant_rows(undo)
        # Assert
        assert has_grant(sender=NEW, target=PEER) is True


class TestNoOpRenames:
    def test_renaming_to_the_same_name_touches_nothing(self):
        # Arrange
        grant_send(sender=OLD, target=PEER, note="pin")
        # Act
        undo = rename_grant_rows(old=OLD, new=OLD)
        # Assert
        assert undo.total == 0

    def test_an_agent_with_no_grants_reports_nothing(self):
        # Arrange
        # Act
        counts = count_grant_rename_rows(old="never-granted-anything")
        # Assert
        assert counts == {}


# EOF
