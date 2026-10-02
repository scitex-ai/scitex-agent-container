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
        name="resident-native", harness="codex", runtime="tui", workdir=str(tmp_path),
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


@pytest.mark.parametrize("failure", ["missing-executable", "rejected-admission"])
def test_direct_production_replacement_leaves_resident_tui_alive(
    tmp_path: Path, env_save_restore, failure: str,
) -> None:
    # Arrange — a real child CLI fixture declines admission without networking.
    config = _native_config(tmp_path, env_save_restore)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    env_save_restore.set("PATH", str(binary_dir))
    if failure == "rejected-admission":
        binary = binary_dir / "codex"
        binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        binary.chmod(0o700)
        account = tmp_path / ".scitex/agent-container/accounts/openai/fixture-account/auth.json"
        account.parent.mkdir(parents=True)
        account.write_text('{"fixture":true}', encoding="utf-8")
    resident = _ResidentMultiplexer()
    runtime = TuiSessionRuntime(multiplexer=resident)
    # Act — production admission runs before this runtime's force-stop.
    with pytest.raises(ProviderEnvError):
        runtime.start(config, force=True)
    # Assert
    assert (resident.alive, resident.stops) == (True, [])


def test_missing_binary_does_not_materialize_private_auth(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange
    config = _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    # Act
    with pytest.raises(ProviderEnvError, match="host PATH"):
        preflight_native_tui(config, production=True)
    # Assert
    assert not (tmp_path / "private-codex-home").exists()


def test_valid_native_admission_uses_a_real_bounded_child_process(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — this local CLI fixture has no network or account fallback.
    config = _native_config(tmp_path, env_save_restore)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text("#!/bin/sh\necho OK\n", encoding="utf-8")
    binary.chmod(0o700)
    env_save_restore.set("PATH", str(binary_dir))
    account = tmp_path / ".scitex/agent-container/accounts/openai/fixture-account/auth.json"
    account.parent.mkdir(parents=True)
    account.write_text('{"fixture":true}', encoding="utf-8")
    # Act
    outcome = preflight_native_tui(config, production=True)
    # Assert
    assert outcome is None


def test_injected_command_builder_keeps_its_existing_launch_seam(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — workspace entry proves admission did not run a host model.
    config = _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    resident = _ResidentMultiplexer()

    class FixtureLaunchReached(Exception):
        pass

    class FixtureRuntime(TuiSessionRuntime):
        def materialize_workspace(self, config):
            raise FixtureLaunchReached

    runtime = FixtureRuntime(
        multiplexer=resident, command_builder=lambda config: ["/fixture/launch"],
    )
    # Act
    with pytest.raises(FixtureLaunchReached):
        runtime.start(config, force=True)
    # Assert
    assert resident.stops == ["tui-resident-native"]


@pytest.mark.parametrize(
    ("harness", "runtime", "production"),
    [("hermes", "tui", True), ("anthropic", "tui", True),
     ("codex", "headless", True), ("codex", "tui", False)],
)
def test_non_production_or_other_harness_admission_is_unchanged(
    tmp_path: Path, env_save_restore, harness: str, runtime: str, production: bool,
) -> None:
    # Arrange
    config = _native_config(tmp_path, env_save_restore)
    config.harness, config.runtime = harness, runtime
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    # Act
    outcome = preflight_native_tui(config, production=production)
    # Assert
    assert outcome is None


def test_default_restart_refuses_before_entering_its_stop_leg(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — use the public production lifecycle, no runtime-factory override.
    _native_config(tmp_path, env_save_restore)
    env_save_restore.set("PATH", str(tmp_path / "absent-bin"))
    spec = tmp_path / "resident-native/spec.yaml"
    spec.parent.mkdir()
    spec.write_text(
        yaml.safe_dump(explicit_doc({
            "harness": "codex", "runtime": "tui", "workdir": str(tmp_path),
            "claude": {"model": ""},
            "engine": "native-fixture",
            "engines": {"native-fixture": {
                "model": "gpt-6.1-sol",
                "subscription": {"provider": "openai", "account": "openai:fixture-account"},
                "reasoning_effort": "ultra", "service_tier": "fast",
            }},
        }), sort_keys=False),
        encoding="utf-8",
    )
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("resident-native", str(spec), "tui-resident-native")
    registered = registry.get("resident-native")
    # Act — a production prerequisite failure must precede agent_stop.
    with pytest.raises(ProviderEnvError, match="host PATH"):
        agent_restart("resident-native", registry=registry)
    # Assert
    assert registry.get("resident-native") == registered
