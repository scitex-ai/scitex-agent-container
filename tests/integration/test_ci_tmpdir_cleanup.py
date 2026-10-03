"""The actual shared cleanup must require an owned, quiescent incarnation."""

from pathlib import Path

import pytest

_CI = Path(__file__).resolve().parents[2] / ".github" / "ci"


@pytest.mark.parametrize("name", ["run-in-sif.sh", "build-in-sif.sh", "publish-in-sif.sh"])
def test_inner_producer_uses_owned_prepare_without_start_deletion(name):
    # Arrange
    path = _CI / name
    # Act
    source = path.read_text()
    # Assert
    assert ('ci_tmpdir_prepare "${TMPDIR:?' in source, 'rm -rf "${TMPDIR:' in source) == (True, False)


def test_inner_test_driver_does_not_sweep_unowned_siblings():
    # Arrange
    path = _CI / "run-in-sif.sh"
    # Act
    source = path.read_text()
    # Assert
    assert "find /tmp" not in source and "-mmin" not in source


def test_shared_prune_uses_nul_paths_and_owned_process_proof():
    # Arrange
    path = _CI / "tmpdir-lib.sh"
    # Act
    source = path.read_text()
    # Assert
    assert '_ci_tmpdir_owned "$d" prune' in source and "-print0" in source


def test_cleanup_removal_is_bounded_and_rechecks_directory_incarnation():
    # Arrange
    path = _CI / "tmpdir-lib.sh"
    # Act
    source = path.read_text()
    # Assert
    assert ("30s rm -rf --one-file-system --" in source, 'stat -c \'%u:%d:%i:%a\'' in source) == (True, True)
