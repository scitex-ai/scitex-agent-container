"""Snapshot read names retain the actual container backend and JSON file exports."""

import hashlib
import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg._main import main


@pytest.fixture
def real_snapshot_store(tmp_path):
    store = tmp_path / "images"
    store.mkdir()
    image = store / "scitex-vowned.sif"
    image.write_bytes(b"owned snapshot hash fixture, not a bootable image")
    (store / "current.sif").symlink_to(image.name)
    lock = store / "requirements.lock"
    lock.write_text("owned-package==1\n")
    saved = ig._CONTAINERS_DIR
    ig._CONTAINERS_DIR = store
    try:
        yield (
            store,
            hashlib.sha256(image.read_bytes()).hexdigest(),
            hashlib.sha256(lock.read_bytes()).hexdigest(),
        )
    finally:
        ig._CONTAINERS_DIR = saved


@pytest.mark.parametrize(
    "command,legacy", [("show-snapshot", False), ("snapshot", True)]
)
@pytest.mark.parametrize(
    "prefix,suffix", [([], []), ([], ["--json"]), (["--json"], [])]
)
def test_snapshot_stdout_uses_real_backend_schema_and_hashes(
    real_snapshot_store, command, legacy, prefix, suffix
):
    # Arrange
    _store, image_hash, lock_hash = real_snapshot_store
    # Act
    result = CliRunner().invoke(main, [*prefix, "image", command, *suffix])
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload["schema_version"],
        payload["container"]["sif_sha256"],
        payload["lock_files"]["pip"],
        payload["dev_repos"],
        "show-snapshot" in result.stderr,
    ) == (0, "1.0", image_hash, lock_hash, [], legacy)


@pytest.mark.parametrize("command", ["show-snapshot", "snapshot"])
def test_snapshot_explicit_file_contains_the_real_payload(
    real_snapshot_store, tmp_path, command
):
    # Arrange
    _store, image_hash, lock_hash = real_snapshot_store
    output = tmp_path / "snapshot.json"
    # Act
    result = CliRunner().invoke(main, ["image", command, "-o", str(output)])
    payload = json.loads(output.read_text())
    # Assert
    assert (
        result.exit_code,
        payload["container"]["sif_sha256"],
        payload["lock_files"]["pip"],
    ) == (0, image_hash, lock_hash)


@pytest.mark.parametrize("command", ["show-snapshot", "snapshot"])
@pytest.mark.parametrize("prefix,suffix", [([], ["--json"]), (["--json"], [])])
def test_snapshot_json_file_ack_is_structured(
    real_snapshot_store, tmp_path, command, prefix, suffix
):
    # Arrange
    output = tmp_path / "snapshot.json"
    # Act
    result = CliRunner().invoke(
        main, [*prefix, "image", command, "-o", str(output), *suffix]
    )
    # Assert
    assert (result.exit_code, json.loads(result.stdout), output.is_file()) == (
        0,
        {"output": str(output)},
        True,
    )


@pytest.mark.parametrize("command", ["show-snapshot", "snapshot"])
def test_snapshot_failed_file_write_never_acknowledges_success(
    real_snapshot_store, tmp_path, command
):
    # Arrange
    output = tmp_path / "missing-parent" / "snapshot.json"
    # Act
    result = CliRunner().invoke(main, ["image", command, "--json", "-o", str(output)])
    # Assert
    assert (result.exit_code, result.stdout, output.exists()) == (1, "", False)
