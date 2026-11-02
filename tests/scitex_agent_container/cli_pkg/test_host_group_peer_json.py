"""Real owned peer registry reads: JSON contains names, never bearer material."""

from __future__ import annotations

import json

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.host_group import host_list_peers


def test_peer_json_reports_sorted_names_without_changing_registry(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    registry = tmp_path / ".scitex" / "agent-container" / "peer-tokens"
    registry.mkdir(parents=True)
    before = {
        "host-b.token": b"owned-fixture-bearer-b",
        "host-a.token": b"owned-fixture-bearer-a",
    }
    for name, data in before.items():
        (registry / name).write_bytes(data)
    # Act
    result = CliRunner().invoke(host_list_peers, ["--json"])
    observation = (
        result.exit_code, json.loads(result.stdout),
        {p.name: p.read_bytes() for p in registry.iterdir()},
    )
    # Assert
    assert observation == (0, {"peers": ["host-a", "host-b"]}, before)


def test_missing_peer_registry_json_is_empty_without_creating_it(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    registry = tmp_path / ".scitex" / "agent-container" / "peer-tokens"
    # Act
    result = CliRunner().invoke(host_list_peers, ["--json"])
    # Assert
    assert (result.exit_code, json.loads(result.stdout), registry.exists()) == (
        0, {"peers": []}, False,
    )


def test_peer_json_honors_existing_parent_json_context(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    # Act
    result = CliRunner().invoke(host_list_peers, [], obj={"json": True})
    # Assert
    assert (result.exit_code, result.stdout) == (0, '{"peers": []}\n')


def test_peer_human_output_keeps_missing_registry_guidance(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    # Act
    result = CliRunner().invoke(host_list_peers, [])
    # Assert
    observation = (
        result.exit_code, "no peer tokens registered" in result.stdout,
        "Add one with:" in result.stdout,
    )
    assert observation == (0, True, True)
