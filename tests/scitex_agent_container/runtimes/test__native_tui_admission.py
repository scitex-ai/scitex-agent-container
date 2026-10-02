"""Native production admission refuses before a resident process is stopped."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._stop import agent_restart
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError
from scitex_agent_container.runtimes._native_tui_admission import preflight_native_tui
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc


def _native_config(tmp_path: Path, env_save_restore) -> AgentConfig:
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("CODEX_HOME", str(tmp_path / "private-codex-home"))
    config = AgentConfig(
        name="resident-native",
        harness="codex",
        runtime="tui",
        workdir=str(tmp_path),
    )
    config.claude.model = "gpt-6.1-sol"
    config.subscription_provider = "openai"
    config.subscription_account = "openai:fixture-account"
    return config


class _ResidentMultiplexer:
    """A resident multiplexer protocol fixture whose stop is observable."""

    def __init__(self) -> None:
        self.alive = True
        self.stops: list[str] = []

    def exists(self, name: str) -> bool:
        return self.alive

    def stop(self, name: str) -> None:
        self.stops.append(name)
        self.alive = False


@pytest.fixture
def missing_executable_config(tmp_path: Path, env_save_restore):
    config = _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    return config


@pytest.fixture
def rejected_admission_config(tmp_path: Path, env_save_restore):
    # A real child CLI fixture declines admission without networking.
    config = _native_config(tmp_path, env_save_restore)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    env_save_restore.set("PATH", str(binary_dir))
    binary = binary_dir / "codex"
    binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    binary.chmod(0o700)
    account = (
        tmp_path / ".scitex/agent-container/accounts/openai/fixture-account/auth.json"
    )
    account.parent.mkdir(parents=True)
    account.write_text('{"fixture":true}', encoding="utf-8")
    return config


@pytest.fixture(params=["missing_executable_config", "rejected_admission_config"])
def rejected_replacement(request):
    config = request.getfixturevalue(request.param)
    resident = _ResidentMultiplexer()
    runtime = TuiSessionRuntime(multiplexer=resident)
    error = None
    try:
        runtime.start(config, force=True)
    except ProviderEnvError as caught:
        error = caught
    return resident, error


def test_direct_production_replacement_refuses_failed_admission(rejected_replacement):
    # Arrange
    _, error = rejected_replacement
    # Act
    refused = isinstance(error, ProviderEnvError)
    # Assert
    assert refused is True


def test_direct_production_replacement_leaves_resident_tui_alive(rejected_replacement):
    # Arrange
    resident, _ = rejected_replacement
    # Act
    alive = resident.alive
    # Assert
    assert alive is True


def test_failed_production_admission_never_enters_force_stop(rejected_replacement):
    # Arrange
    resident, _ = rejected_replacement
    # Act
    stops = resident.stops
    # Assert
    assert stops == []


@pytest.fixture
def missing_binary_preflight(missing_executable_config):
    config = missing_executable_config
    error = None
    try:
        preflight_native_tui(config, production=True)
    except ProviderEnvError as caught:
        error = caught
    return error


def test_missing_binary_refusal_names_the_host_path(missing_binary_preflight):
    # Arrange
    error = missing_binary_preflight
    # Act
    reason = str(error)
    # Assert
    assert "host PATH" in reason


def test_missing_binary_does_not_materialize_private_auth(
    tmp_path: Path,
    missing_binary_preflight,
) -> None:
    # Arrange
    home = tmp_path / "private-codex-home"
    # Act
    exists = home.exists()
    # Assert
    assert exists is False


def test_valid_native_admission_uses_a_real_bounded_child_process(
    tmp_path: Path,
    env_save_restore,
) -> None:
    # Arrange — this local CLI fixture has no network or account fallback.
    config = _native_config(tmp_path, env_save_restore)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text("#!/bin/sh\necho OK\n", encoding="utf-8")
    binary.chmod(0o700)
    env_save_restore.set("PATH", str(binary_dir))
    account = (
        tmp_path / ".scitex/agent-container/accounts/openai/fixture-account/auth.json"
    )
    account.parent.mkdir(parents=True)
    account.write_text('{"fixture":true}', encoding="utf-8")
    # Act
    outcome = preflight_native_tui(config, production=True)
    # Assert
    assert outcome is None


class FixtureLaunchReached(Exception):
    pass


@pytest.fixture
def injected_launch_attempt(
    tmp_path: Path,
    env_save_restore,
) -> tuple[_ResidentMultiplexer, FixtureLaunchReached | None]:
    # Arrange — workspace entry proves admission did not run a host model.
    config = _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    resident = _ResidentMultiplexer()

    class FixtureRuntime(TuiSessionRuntime):
        def materialize_workspace(self, config):
            raise FixtureLaunchReached

    runtime = FixtureRuntime(
        multiplexer=resident,
        command_builder=lambda config: ["/fixture/launch"],
    )
    error = None
    try:
        runtime.start(config, force=True)
    except FixtureLaunchReached as caught:
        error = caught
    return resident, error


def test_injected_command_builder_keeps_its_existing_launch_seam(
    injected_launch_attempt,
):
    # Arrange
    _, error = injected_launch_attempt
    # Act
    reached = isinstance(error, FixtureLaunchReached)
    # Assert
    assert reached is True


def test_injected_launch_seam_keeps_the_requested_resident_replacement(
    injected_launch_attempt,
):
    # Arrange
    resident, _ = injected_launch_attempt
    # Act
    stops = resident.stops
    # Assert
    assert stops == ["tui-resident-native"]


@pytest.mark.parametrize(
    ("harness", "runtime", "production"),
    [
        ("hermes", "tui", True),
        ("anthropic", "tui", True),
        ("codex", "headless", True),
        ("codex", "tui", False),
    ],
)
def test_non_production_or_other_harness_admission_is_unchanged(
    tmp_path: Path,
    env_save_restore,
    harness: str,
    runtime: str,
    production: bool,
) -> None:
    # Arrange
    config = _native_config(tmp_path, env_save_restore)
    config.harness, config.runtime = harness, runtime
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    # Act
    outcome = preflight_native_tui(config, production=production)
    # Assert
    assert outcome is None


@pytest.fixture
def rejected_default_restart(
    tmp_path: Path,
    env_save_restore,
):
    # Arrange — use the public production lifecycle, no runtime-factory override.
    _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    spec = tmp_path / "resident-native/spec.yaml"
    spec.parent.mkdir()
    spec.write_text(
        yaml.safe_dump(
            explicit_doc(
                {
                    "harness": "codex",
                    "runtime": "tui",
                    "workdir": str(tmp_path),
                    "claude": {"model": ""},
                    "engine": "native-fixture",
                    "engines": {
                        "native-fixture": {
                            "model": "gpt-6.1-sol",
                            "subscription": {
                                "provider": "openai",
                                "account": "openai:fixture-account",
                            },
                            "reasoning_effort": "ultra",
                            "service_tier": "fast",
                        }
                    },
                }
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("resident-native", str(spec), "tui-resident-native")
    registered = registry.get("resident-native")
    error = None
    try:
        agent_restart("resident-native", registry=registry)
    except ProviderEnvError as caught:
        error = caught
    return registry, registered, error


def test_default_restart_refuses_before_entering_its_stop_leg(rejected_default_restart):
    # Arrange
    _, _, error = rejected_default_restart
    # Act
    refused = isinstance(error, ProviderEnvError)
    # Assert
    assert refused is True


def test_default_restart_refusal_names_the_host_path(rejected_default_restart):
    # Arrange
    _, _, error = rejected_default_restart
    # Act
    reason = str(error)
    # Assert
    assert "host PATH" in reason


def test_rejected_default_restart_preserves_the_registry_row(rejected_default_restart):
    # Arrange
    registry, registered, _ = rejected_default_restart
    # Act
    current = registry.get("resident-native")
    # Assert
    assert current == registered
