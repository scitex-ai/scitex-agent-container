"""Retained helper claims are refused before a stop or replacement is possible."""

import os

import pytest

from scitex_agent_container.runtimes._tui_recovery_guard import refuse_existing_helper


@pytest.mark.parametrize("kind", ["regular", "broken-symlink", "fifo", "directory"])
def test_any_retained_claim_type_refuses_without_reading_or_signalling(tmp_path, kind):
    # Arrange
    path = tmp_path / "helper.pid"
    _create_retained_claim(path, kind)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="existing bridge claim"):
        refuse_existing_helper(path, "bridge")


def _create_retained_claim(path, kind):
    if kind == "regular":
        path.write_text("2153295")
    elif kind == "broken-symlink":
        path.symlink_to(path.parent / "absent")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()


def test_missing_helper_claim_is_not_fabricated(tmp_path):
    # Arrange
    path = tmp_path / "absent.pid"
    # Act
    refuse_existing_helper(path, "bridge")
    # Assert
    assert path.exists() is False
