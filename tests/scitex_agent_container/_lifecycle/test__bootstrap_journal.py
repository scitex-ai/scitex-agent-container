"""Actual exclusive files, symlink refusals, and durable consumed attempts."""

import json
import stat

import pytest

from scitex_agent_container._lifecycle._bootstrap_journal import BootstrapJournal

LAUNCH = "932ebcea-3619-4e60-a6d2-7069a1381815"


def test_real_journal_receipt_has_private_regular_file_metadata(tmp_path):
    # Arrange
    journal = BootstrapJournal(tmp_path, LAUNCH)
    # Act
    try:
        journal.write("claim", {"launch_id": LAUNCH})
        mode = stat.S_IMODE((journal.path / "claim.json").stat().st_mode)
    finally:
        journal.close()
    # Assert
    assert mode == 0o600


def test_consumed_attempt_directory_prevents_a_second_claim(tmp_path):
    # Arrange
    first = BootstrapJournal(tmp_path, LAUNCH)
    first.close()
    # Act
    # Assert
    with pytest.raises(FileExistsError):
        BootstrapJournal(tmp_path, LAUNCH)


def test_receipt_cannot_overwrite_a_prior_partial_phase(tmp_path):
    # Arrange
    journal = BootstrapJournal(tmp_path, LAUNCH)
    journal.write("result", {"completed": False})
    # Act
    try:
        # Assert
        with pytest.raises(FileExistsError):
            journal.write("result", {"completed": True})
    finally:
        journal.close()


def test_receipt_cannot_overwrite_a_prior_partial_phase_preserves_state(tmp_path):
    # Arrange
    journal = BootstrapJournal(tmp_path, LAUNCH)
    journal.write("result", {"completed": False})
    # Act
    try:
        try:
            journal.write("result", {"completed": True})
        except FileExistsError:
            pass
        retained = json.loads((journal.path / "result.json").read_text())
    finally:
        journal.close()
    # Assert
    assert retained == {"completed": False}


def test_symlink_state_parent_is_refused_before_any_claim(tmp_path):
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # Act
    # Assert
    with pytest.raises(OSError):
        BootstrapJournal(alias, LAUNCH)


def test_symlink_state_parent_is_refused_before_any_claim_preserves_state(tmp_path):
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # Act
    try:
        BootstrapJournal(alias, LAUNCH)
    except OSError:
        pass
    # Assert
    assert list(real.iterdir()) == []
