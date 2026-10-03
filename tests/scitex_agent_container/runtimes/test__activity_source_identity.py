"""Opaque source equality must bind every authoritative identity component."""

import pytest

from scitex_agent_container.runtimes._activity_source_identity import activity_source_id


@pytest.fixture
def target():
    return {
        "agent": "worker",
        "host": "host",
        "instance_id": "instance",
        "boot_id": "boot",
        "session_id": "session",
    }


@pytest.mark.parametrize(
    "key", ["agent", "host", "instance_id", "boot_id", "session_id"]
)
def test_each_authority_rollover_changes_source_equality(target, key):
    # Arrange
    changed = {**target, key: "successor"}
    # Act
    before = activity_source_id(target, (1, 2))
    after = activity_source_id(changed, (1, 2))
    # Assert
    assert before != after


def test_replacement_inode_does_not_reuse_source_equality(target):
    # Arrange
    # Act
    before = activity_source_id(target, (1, 2))
    after = activity_source_id(target, (1, 3))
    # Assert
    assert before != after


@pytest.mark.parametrize(
    "metadata", [None, (), (1,), (True, 2), (1, -1), ("private/path", 2)]
)
def test_unverified_metadata_is_refused(target, metadata):
    # Arrange
    # Act
    # Assert
    with pytest.raises(ValueError, match="owned device/inode"):
        activity_source_id(target, metadata)


def test_extra_arbitrary_authority_path_is_refused(target):
    # Arrange
    target["path"] = "/private/source"
    # Act
    # Assert
    with pytest.raises(ValueError, match="exact native authority"):
        activity_source_id(target, (1, 2))
