"""Real filesystem controls for an explicitly denied automatic footprint."""

from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._apptainer_bind_guard import spec_binds_checked
from scitex_agent_container.runtimes._isolated_footprint import (
    IsolationFootprintError,
    automatic_store_denied,
    validate_neutralizer_binds,
)
from scitex_agent_container.runtimes._p3a_default_binds import apply_default_binds
from scitex_agent_container.runtimes._to_home import deploy_to_home


def _restricted_config(tmp_path: Path) -> AgentConfig:
    config = AgentConfig(name="restricted-child")
    config.config_path = str(tmp_path / "spec.yaml")
    config.env = {"SCITEX_STORE_DSN": "", "PGUSER": "", "PGPASSFILE": ""}
    config.to_home_layers = ["per-agent"]
    return config


def test_three_present_empty_declarations_deny_automatic_authority(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    # Act
    denied = automatic_store_denied(config)
    # Assert
    assert denied is True


@pytest.mark.parametrize("missing", ["SCITEX_STORE_DSN", "PGUSER", "PGPASSFILE"])
def test_missing_declaration_does_not_infer_denial(tmp_path, missing):
    # Arrange
    config = _restricted_config(tmp_path)
    config.env.pop(missing)
    # Act
    denied = automatic_store_denied(config)
    # Assert
    assert denied is False


@pytest.mark.parametrize("value", [" ", None, False, "declared-value"])
def test_non_literal_empty_declaration_does_not_infer_denial(tmp_path, value):
    # Arrange
    config = _restricted_config(tmp_path)
    config.env["PGUSER"] = value
    # Act
    denied = automatic_store_denied(config)
    # Assert
    assert denied is False


@pytest.mark.parametrize("destination", [
    "/home/agent/.scitex/cards", "/home/agent/.scitex/claude-code-telegrammer",
    "/tmp/emacs-claude-code", "/tmp/host",
])
def test_declared_readonly_empty_neutralizer_is_accepted(tmp_path, destination):
    # Arrange
    config = _restricted_config(tmp_path)
    empty = tmp_path / "cards"
    empty.mkdir()
    config.apptainer.binds = [f"{empty}:{destination}:ro"]
    # Act
    result = validate_neutralizer_binds(config)
    # Assert
    assert result is None


def test_missing_neutralizer_refuses_before_home_projection(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    config.apptainer.binds = [f"{tmp_path / 'missing'}:/home/agent/.scitex/cards:ro"]
    # Act
    # Assert
    with pytest.raises(IsolationFootprintError, match="neutralizer-missing-or-not-directory"):
        deploy_to_home(config, str(tmp_path / "home"))


def test_missing_neutralizer_is_not_repaired(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    missing = tmp_path / "missing"
    config.apptainer.binds = [f"{missing}:/home/agent/.scitex/cards:ro"]
    # Act
    try:
        validate_neutralizer_binds(config)
    except IsolationFootprintError:
        pass
    # Assert
    assert not missing.exists()


def test_missing_neutralizer_refuses_capability_bind_admission(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    config.apptainer.binds = [f"{tmp_path / 'missing'}:/tmp/host:ro"]
    # Act
    # Assert
    with pytest.raises(IsolationFootprintError, match="neutralizer-missing-or-not-directory"):
        spec_binds_checked(config)


def test_nonempty_neutralizer_refuses_without_reading_contents(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    directory = tmp_path / "cards"
    directory.mkdir()
    (directory / "retained").write_text("retained source bytes")
    config.apptainer.binds = [f"{directory}:/home/agent/.scitex/cards:ro"]
    # Act
    # Assert
    with pytest.raises(IsolationFootprintError, match="neutralizer-not-empty"):
        validate_neutralizer_binds(config)


def test_refusal_preserves_existing_home_projection(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    config.apptainer.binds = [f"{tmp_path / 'missing'}:/tmp/host:ro"]
    home = tmp_path / "home"
    home.mkdir()
    mcp = home / ".mcp.json"
    mcp.write_text("prior projection")
    # Act
    try:
        deploy_to_home(config, str(home))
    except IsolationFootprintError:
        pass
    # Assert
    assert mcp.read_text() == "prior projection"


def test_a_neutralizer_becoming_nonempty_is_rechecked(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    directory = tmp_path / "empty"
    directory.mkdir()
    config.apptainer.binds = [f"{directory}:/tmp/emacs-claude-code:ro"]
    validate_neutralizer_binds(config)
    (directory / "new-entry").write_text("retained")
    # Act
    # Assert
    with pytest.raises(IsolationFootprintError, match="neutralizer-not-empty"):
        validate_neutralizer_binds(config)


def test_normal_agent_keeps_deliberate_nonempty_readonly_mount(tmp_path):
    # Arrange
    config = AgentConfig(name="normal-agent")
    directory = tmp_path / "cards"
    directory.mkdir()
    (directory / "declared-data").write_text("retained")
    config.apptainer.binds = [f"{directory}:/home/agent/.scitex/cards:ro"]
    # Act
    result = validate_neutralizer_binds(config)
    # Assert
    assert result is None


def test_restricted_contract_preserves_deliberate_writable_mount(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    config.apptainer.binds = [f"{tmp_path / 'declared'}:/home/agent/.scitex/cards:rw"]
    # Act
    result = validate_neutralizer_binds(config)
    # Assert
    assert result is None


def test_restricted_contract_adds_no_implicit_fleet_bind(tmp_path):
    # Arrange
    config = _restricted_config(tmp_path)
    explicit = [f"{tmp_path}:/work/input:ro"]
    # Act
    result = apply_default_binds(explicit, config=config)
    # Assert
    assert result == explicit


def test_unrestricted_default_host_tmp_bind_is_preserved():
    # Arrange
    config = AgentConfig(name="normal-agent")
    # Act
    result = apply_default_binds([], config=config)
    # Assert
    assert "/tmp:/tmp/host:ro" in result
