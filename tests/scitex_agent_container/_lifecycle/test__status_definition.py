"""Real local specs and owned process evidence precede foreign placement rows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle import _verdict_remote
from scitex_agent_container._lifecycle._status import agent_status
from scitex_agent_container._lifecycle._status_definition import defined_status
from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    INSTRUMENT_HOST_TMUX,
    INSTRUMENT_PID_NAMESPACE,
    SOURCE_PROCESS,
    UNKNOWN,
    Signal,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import load_config
from scitex_agent_container.config._resolve import AmbiguousRegistryScope
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


class ProcessRuntime:
    """Observe the owned PID recorded under the real configured workdir."""

    def __init__(self, config):
        self.pid_path = Path(config.expanded_workdir) / "owned.pid"

    def is_running(self, config):
        pid = int(self.pid_path.read_text())
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


def process_probe(config, runtime):
    """The fixture's actual same-namespace PID is independent of fleet rows."""
    alive = runtime.is_running(config)
    return Signal(SOURCE_PROCESS, ALIVE if alive else DEAD,
                  "owned same-namespace process probe", INSTRUMENT_PID_NAMESPACE)


def failed_runtime(config):
    raise RuntimeError("private-fixture-error-body")


def foreign_rows():
    return [{"id": "old-foreign-id", "name": "paper", "host": "windows-peer",
             "screen": "old-foreign-session", "started_at": "2026-09-01T00:00:00Z",
             "remote": True, "bound_port": 19001, "compiled_spec_json": "private-birth"}]


def failed_rows():
    raise RuntimeError("private-placement-error-body")


def write_spec(agents, workdir, name="paper"):
    path = agents / name / "spec.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(explicit_doc({
        "host": "${HOSTNAME}", "runtime": "tui", "harness": "codex",
        "workdir": str(workdir), "engine": "native-fixture",
        "available_engines": {"native-fixture": {
            "harness": "codex", "model": "gpt-6.1-sol",
            "subscription": {"provider": "openai", "account": "openai:fixture"},
        }},
    })))
    return path


@pytest.fixture
def scope(tmp_path, env_save_restore):
    home = tmp_path / "home"
    fleet = home / ".scitex/agent-container/agents"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_DIR", str(home / ".scitex"))
    # Keep the import-time runtime-state constant under conftest's per-worker
    # safety floor. A tmp_path override is too late when the status adapter is
    # imported lazily, and the teardown guard correctly rejects that escape.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "current-node")
    env_save_restore.delete("SAC_AGENT_SCOPE")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    before = Path.cwd()
    os.chdir(project)
    try:
        yield fleet, workdir, project, Registry(registry_dir=tmp_path / "registry")
    finally:
        os.chdir(before)


@pytest.fixture
def stopped_definition(scope):
    fleet, workdir, _, registry = scope
    path = write_spec(fleet, workdir)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    (workdir / "owned.pid").write_text(str(process.pid))
    return path, registry


@pytest.fixture
def live_definition(scope):
    fleet, workdir, _, registry = scope
    path = write_spec(fleet, workdir)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (workdir / "owned.pid").write_text(str(process.pid))
    try:
        yield path, registry
    finally:
        process.terminate()
        process.wait(timeout=5)


def read_local(definition, *, runtime_factory=ProcessRuntime, instance_reader=lambda: []):
    _, registry = definition
    return agent_status("paper", registry=registry, runtime_factory=runtime_factory,
                        instance_reader=instance_reader, process_probe=process_probe)


def test_never_registered_local_spec_is_a_valid_definition(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["observation"]["definition"]["state"] == "valid"


def test_discovered_local_spec_keeps_its_authoritative_path(stopped_definition):
    # Arrange
    path, _ = stopped_definition
    # Act
    result = read_local(stopped_definition)
    # Assert
    assert result["config"] == str(path)


def test_unregistered_definition_works_through_default_readers(scope):
    # Arrange
    fleet, workdir, _, registry = scope
    path = write_spec(fleet, workdir)
    # Act
    result = agent_status("paper", registry=registry, runtime_factory=failed_runtime)
    # Assert
    assert result["config"] == str(path)


def test_discovered_spec_publishes_configured_harness(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["harness"] == "codex"


def test_discovered_spec_publishes_exact_selected_engine(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["engine"] == "native-fixture"


def test_unregistered_live_process_identity_remains_spec_intent(live_definition):
    # Arrange
    definition = live_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["runtime_identity_source"] == "spec"


def test_local_reaped_process_reports_local_absence(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["observation"]["process"]["state"] == "absent"


def test_placement_row_remains_unknown_without_remote_process_evidence(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=foreign_rows)
    # Assert
    assert result["placement_evidence"]["records"][0]["liveness"] == "unknown"


def test_old_foreign_host_is_preserved_as_placement(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=foreign_rows)
    # Assert
    assert result["placement_evidence"]["records"][0]["host"] == "windows-peer"


def test_latest_instance_host_owns_remote_process_probe(scope, monkeypatch):
    # Arrange
    fleet, workdir, _, _ = scope
    path = write_spec(fleet, workdir)
    config = load_config(path)
    config.hosts_spec.host = "current-node"  # stale spec intent loses to launch row
    resolved_hosts = []
    remote_peers = []

    def resolve_host(host):
        resolved_hosts.append(host)
        return "windows-peer"

    def remote_probe(_config, peer):
        remote_peers.append(peer)
        return Signal(
            SOURCE_PROCESS, ALIVE, "remote owner tmux session is alive",
            INSTRUMENT_HOST_TMUX,
        )

    monkeypatch.setattr(_verdict_remote, "_remote_peer_for_host", resolve_host)
    # Act
    result = defined_status(
        "paper", str(path), config,
        runtime_factory=lambda _config: pytest.fail("local runtime must not be probed"),
        instance_reader=foreign_rows,
        remote_process_probe=remote_probe,
    )
    # Assert
    assert resolved_hosts == ["windows-peer"]
    assert remote_peers == ["windows-peer"]
    assert result["host"] == "current-node"
    assert result["process_observation_scope"] == "remote"
    assert result["process_observation_host"] == "windows-peer"
    assert result["liveness"]["verdict"] == ALIVE
    assert result["status"] == "running"


def test_unresolvable_instance_host_is_unknown_without_local_probe(scope, monkeypatch):
    # Arrange
    fleet, workdir, _, _ = scope
    path = write_spec(fleet, workdir)
    config = load_config(path)

    def unresolved(_host):
        raise RuntimeError("peer mapping unavailable")

    monkeypatch.setattr(_verdict_remote, "_remote_peer_for_host", unresolved)
    # Act
    result = defined_status(
        "paper", str(path), config,
        runtime_factory=lambda _config: pytest.fail("must not probe locally"),
        instance_reader=foreign_rows,
    )
    # Assert
    assert result["process_observation_scope"] == "unknown"
    assert result["liveness"]["verdict"] == UNKNOWN
    assert result["status"] == "unknown"


def test_foreign_row_cannot_replace_current_local_host(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=foreign_rows)
    # Assert
    assert result["host"] == "current-node"


def test_foreign_row_cannot_supply_local_session(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=foreign_rows)
    # Assert
    assert result["screen"] == ""


def test_foreign_row_cannot_supply_local_start_time(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=foreign_rows)
    # Assert
    assert result["started_at"] == ""


def test_foreign_row_cannot_supply_local_bound_endpoint(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["a2a"]["resolved_port"] is None


def test_positive_owned_process_is_observed_running(live_definition):
    # Arrange
    definition = live_definition
    # Act
    result = read_local(definition)
    # Assert
    assert result["status"] == "running"


def test_status_read_does_not_register_the_local_definition(stopped_definition):
    # Arrange
    _, registry = stopped_definition
    # Act
    read_local(stopped_definition)
    # Assert
    assert registry.get("paper") is None


def test_runtime_probe_failure_is_unknown_with_valid_definition(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, runtime_factory=failed_runtime)
    # Assert
    assert result["observation"]["process"]["state"] == "unknown"


def test_runtime_failure_does_not_leak_private_error_body(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, runtime_factory=failed_runtime)
    # Assert
    assert "private-fixture-error-body" not in json.dumps(result)


def test_placement_failure_is_not_an_empty_fleet_claim(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition, instance_reader=failed_rows)
    # Assert
    assert result["placement_evidence"]["state"] == "unknown"


def test_duplicate_placements_are_retained_without_newest_wins(stopped_definition):
    # Arrange
    rows = foreign_rows() + [{"id": "other-id", "name": "paper", "host": "other-peer"}]
    # Act
    result = read_local(stopped_definition, instance_reader=lambda: rows)
    # Assert
    assert [row["id"] for row in result["placement_evidence"]["records"]] == ["old-foreign-id", "other-id"]


def test_placement_projection_excludes_compiled_birth_payload(stopped_definition):
    # Arrange
    definition = stopped_definition
    # Act
    result = read_local(definition)
    # Assert
    assert "private-birth" not in json.dumps(result)


def test_invalid_discovered_spec_cannot_fall_back_to_foreign_row(scope):
    # Arrange
    fleet, workdir, _, registry = scope
    path = write_spec(fleet, workdir)
    path.write_text("apiVersion: unsupported\n")
    # Act
    refused = pytest.raises(ValueError, match="Config validation failed")
    # Assert
    with refused:
        agent_status("paper", registry=registry, instance_reader=foreign_rows)


def test_ambiguous_local_spec_cannot_fall_back_to_foreign_row(scope):
    # Arrange
    fleet, workdir, project, registry = scope
    write_spec(fleet, workdir)
    write_spec(project / ".scitex/agent-container/agents", workdir)
    # Act
    refused = pytest.raises(AmbiguousRegistryScope, match="BOTH registries")
    # Assert
    with refused:
        agent_status("paper", registry=registry, instance_reader=foreign_rows)


def test_explicit_scope_resolves_the_valid_local_collision(scope, env_save_restore):
    # Arrange
    fleet, workdir, project, registry = scope
    path = write_spec(fleet, workdir)
    write_spec(project / ".scitex/agent-container/agents", workdir)
    env_save_restore.set("SAC_AGENT_SCOPE", "user")
    # Act
    result = agent_status("paper", registry=registry, runtime_factory=failed_runtime,
                          instance_reader=foreign_rows)
    # Assert
    assert result["config"] == str(path)


def test_no_local_definition_preserves_remote_fallback(scope):
    # Arrange
    _, _, _, registry = scope
    # Act
    result = agent_status("paper", registry=registry, instance_reader=foreign_rows,
                          heartbeat_reader=lambda: [])
    # Assert
    assert result["host"] == "windows-peer"


def test_remote_only_definition_remains_missing(scope):
    # Arrange
    _, _, _, registry = scope
    # Act
    result = agent_status("paper", registry=registry, instance_reader=foreign_rows,
                          heartbeat_reader=lambda: [])
    # Assert
    assert result["observation"]["definition"]["state"] == "missing"


def test_no_definition_or_placement_retains_not_found_error(scope):
    # Arrange
    _, _, _, registry = scope
    # Act
    refused = pytest.raises(RuntimeError, match="not found in registry")
    # Assert
    with refused:
        agent_status("paper", registry=registry, instance_reader=lambda: [],
                     heartbeat_reader=lambda: [])


def test_registered_incarnation_retains_exact_spec_priority(scope):
    # Arrange
    fleet, workdir, project, registry = scope
    registered = write_spec(project / "registered-authority/agents", workdir)
    discovered = write_spec(fleet, workdir)
    discovered.write_text("apiVersion: unsupported\n")
    registry.add("paper", str(registered), "registered-session")
    # Act
    result = agent_status("paper", registry=registry, runtime_factory=failed_runtime)
    # Assert
    assert result["config"] == str(registered)


def test_explicit_search_extension_discovers_unregistered_spec(scope, env_save_restore):
    # Arrange
    _, workdir, project, registry = scope
    extension = project / "operator-extension"
    path = write_spec(extension, workdir)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extension))
    # Act
    result = agent_status("paper", registry=registry, runtime_factory=failed_runtime,
                          instance_reader=foreign_rows)
    # Assert
    assert result["config"] == str(path)
