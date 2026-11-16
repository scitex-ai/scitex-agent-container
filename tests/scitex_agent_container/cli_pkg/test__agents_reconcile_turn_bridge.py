"""CLI approval and JSON contracts for bridge-only repair."""

import importlib
import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main
from scitex_agent_container.cli_pkg.agent_group import agent_group
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._tui_turn_bridge_reconcile import (
    TurnBridgeReconcileResult,
)


@pytest.fixture
def bridge_recorder():
    module = importlib.import_module(
        "scitex_agent_container.cli_pkg._agents_reconcile_turn_bridge"
    )
    calls = []
    originals = {
        name: getattr(module, name)
        for name in (
            "resolve_with_prefix",
            "load_config",
            "resolve_hostname",
            "reconcile_turn_bridge",
        )
    }

    def resolve(name):
        calls.append(("resolve", name))
        return name

    def load(name):
        calls.append(("load", name))
        return AgentConfig(name=name, runtime="tui")

    def reconcile(config, *, apply, current_host):
        calls.append(("reconcile", config.name, apply, current_host))
        return TurnBridgeReconcileResult(
            name=config.name,
            status="observed",
            port=19000,
            source="instance_row",
            session="worker-session",
            prior_bridge_pid=111,
            bridge_pid=111,
            tui_preserved=True,
            applied=apply,
        )

    module.resolve_with_prefix = resolve
    module.load_config = load
    module.resolve_hostname = lambda: "owned-test-host"
    module.reconcile_turn_bridge = reconcile
    try:
        yield calls
    finally:
        for name, original in originals.items():
            setattr(module, name, original)


@pytest.mark.parametrize("approval", ["--apply", "--yes", "-y"])
def test_bridge_conflicting_flags_refuse_before_identity(bridge_recorder, approval):
    # Arrange
    args = ["reconcile-turn-bridge", "worker", "--dry-run", approval]
    # Act
    result = CliRunner().invoke(agent_group, args)
    # Assert
    assert (
        result.exit_code,
        "cannot be combined" in result.output,
        bridge_recorder,
    ) == (2, True, [])


@pytest.mark.parametrize(
    "flags, applied",
    [
        ([], False),
        (["--dry-run"], False),
        (["--apply"], True),
        (["--yes"], True),
        (["-y"], True),
    ],
)
def test_bridge_forwards_approval(bridge_recorder, flags, applied):
    # Arrange
    args = ["reconcile-turn-bridge", "worker", "--json", *flags]
    # Act
    result = CliRunner().invoke(agent_group, args)
    # Assert
    assert (
        result.exit_code,
        json.loads(result.output)["applied"],
        bridge_recorder,
    ) == (
        0,
        applied,
        [
            ("resolve", "worker"),
            ("load", "worker"),
            ("reconcile", "worker", applied, "owned-test-host"),
        ],
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--json", "agents", "reconcile-turn-bridge", "worker"],
        ["agents", "reconcile-turn-bridge", "worker", "--json"],
    ],
)
def test_bridge_json_output_honors_local_and_root_flag(bridge_recorder, args):
    # Arrange
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.output)) == (
        0,
        {
            "name": "worker",
            "status": "observed",
            "port": 19000,
            "source": "instance_row",
            "session": "worker-session",
            "prior_bridge_pid": 111,
            "bridge_pid": 111,
            "tui_preserved": True,
            "applied": False,
        },
    )


@pytest.mark.parametrize("flag", ["--dry-run", "--yes", "-y", "--apply", "--json"])
def test_bridge_help_documents_supported_flags(flag):
    # Arrange
    # Act
    result = CliRunner().invoke(agent_group, ["reconcile-turn-bridge", "--help"])
    # Assert
    assert (result.exit_code, flag in result.output) == (0, True)


@pytest.fixture
def refusing_bridge(bridge_recorder):
    module = importlib.import_module(
        "scitex_agent_container.cli_pkg._agents_reconcile_turn_bridge"
    )

    def refuse(config, **kwargs):
        raise RuntimeError("no authoritative live endpoint")

    module.reconcile_turn_bridge = refuse


@pytest.mark.parametrize(
    "args",
    [
        ["--json", "agents", "reconcile-turn-bridge", "worker", "--yes"],
        ["agents", "reconcile-turn-bridge", "worker", "--yes", "--json"],
    ],
)
def test_bridge_backend_refusal_remains_json_failure(refusing_bridge, args):
    # Arrange
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.output)) == (
        1,
        {
            "name": "worker",
            "status": "error",
            "error": "no authoritative live endpoint",
        },
    )


def test_bridge_backend_refusal_remains_text_failure(refusing_bridge):
    # Arrange
    args = ["reconcile-turn-bridge", "worker", "--yes"]
    # Act
    result = CliRunner().invoke(agent_group, args)
    # Assert
    assert (result.exit_code, result.output.strip()) == (
        1,
        "Error: no authoritative live endpoint",
    )
