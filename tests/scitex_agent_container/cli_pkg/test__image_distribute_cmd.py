"""CLI contract tests for ``sac image distribute``."""

from __future__ import annotations

import json

from click.testing import CliRunner

from scitex_agent_container.cli_pkg import _image_distribute_cmd as cmd
from scitex_agent_container.cli_pkg.image_group import image_group


def test_distribute_is_registered_without_an_alias():
    # Arrange
    commands = image_group.commands
    # Act
    observed = ("distribute" in commands, "deploy" in commands)
    # Assert
    assert observed == (True, False)


def test_unknown_target_is_rejected_before_transport(tmp_path):
    # Arrange
    source = tmp_path / "x.sif"
    source.write_bytes(b"x")
    old_load = cmd._load_config

    class Config:
        peers = {}
        source_path = tmp_path / "config.yaml"

    cmd._load_config = lambda: Config()
    # Act
    try:
        result = CliRunner().invoke(
            image_group,
            ["distribute", str(source), "--layer", "base", "--host", "ghost"],
        )
    finally:
        cmd._load_config = old_load
    # Assert
    assert (result.exit_code, "not configured" in result.output) == (2, True)


def test_json_dry_run_resolves_artifact_but_opens_no_transport(tmp_path):
    # Arrange
    source = tmp_path / "x.sif"
    source.write_bytes(b"x")
    old_load, old_transport = cmd._load_config, cmd._transport

    class Config:
        peers = {"compute-01": object()}
        source_path = tmp_path / "config.yaml"

    cmd._load_config = lambda: Config()
    cmd._transport = lambda peers, timeout: (_ for _ in ()).throw(
        AssertionError("dry-run constructed a live transport")
    )
    # Act
    try:
        result = CliRunner().invoke(
            image_group,
            [
                "distribute",
                str(source),
                "--layer",
                "base",
                "--host",
                "compute-01",
                "--dry-run",
                "--json",
            ],
        )
    finally:
        cmd._load_config, cmd._transport = old_load, old_transport
    # Assert
    observed = (
        result.exit_code,
        '"status": "planned"' in result.output,
        '"dry_run": true' in result.output,
    )
    assert observed == (0, True, True), result.output


def test_requested_receipt_is_written_as_structured_json(tmp_path):
    # Arrange
    receipt = tmp_path / "audit" / "receipt.json"
    payload = {
        "schema": "sac.image.distribution-receipt/v1",
        "hosts": [{"host": "compute-01", "sha256": "a" * 64, "size": 3}],
    }
    # Act
    cmd._write_receipt(receipt, payload)
    observed = json.loads(receipt.read_text())
    # Assert
    assert observed == payload
