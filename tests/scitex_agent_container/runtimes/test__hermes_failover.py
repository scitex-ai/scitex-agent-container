"""Declared account fairness retains native pool exhaustion fences."""

from __future__ import annotations

import json
import os

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes._hermes_failover import (
    configure_failover,
    materialize_pools,
    spread_declared_accounts,
)


@pytest.fixture
def declared_pool(tmp_path):
    # Arrange: only synthetic, explicitly declared credentials are visible.
    saved = {name: os.environ.get(name) for name in ("TEST_GO_ONE", "TEST_GO_TWO")}
    os.environ.update(TEST_GO_ONE="synthetic-one", TEST_GO_TWO="synthetic-two")
    config = AgentConfig(name="scitex-app", runtime="tui", harness="hermes")
    config.engine_key = "scitex-free"
    config.model = "muse-spark-1.3-contributor"
    config.max_context_tokens = 1_048_576
    config.workdir = str(tmp_path)
    config.claude.provider = ProviderSpec(
        base_url="", auth_token_env="OPENCODE_GO_API_KEY", hermes_provider="opencode-go"
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={"scitex-free": ["TEST_GO_ONE", "TEST_GO_TWO"]}
    )
    rendered = {"providers": {}, "fallback_providers": []}
    try:
        yield config, rendered
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_multi_account_pool_uses_native_round_robin(declared_pool):
    # Arrange
    config, rendered = declared_pool
    # Act
    configure_failover(config, rendered)
    # Assert
    assert rendered["credential_pool_strategies"] == {"opencode-go": "round_robin"}


def test_single_account_keeps_existing_strategy(declared_pool):
    # Arrange
    config, rendered = declared_pool
    config.hermes_failover.accounts["scitex-free"] = ["TEST_GO_TWO"]
    # Act
    configure_failover(config, rendered)
    # Assert
    assert rendered["credential_pool_strategies"] == {"opencode-go": "fill_first"}


def test_duplicate_alias_does_not_become_two_accounts(declared_pool):
    # Arrange
    config, rendered = declared_pool
    os.environ["TEST_GO_TWO"] = "synthetic-one"
    # Act
    configure_failover(config, rendered)
    # Assert
    assert rendered["credential_pool_strategies"] == {"opencode-go": "fill_first"}


def test_initial_position_spreads_existing_accounts():
    # Arrange
    rows = [{"label": "TEST_GO_ONE"}, {"label": "TEST_GO_TWO"}]
    # Act
    first = {
        spread_declared_accounts(f"agent-{index}", "opencode-go", rows)[0]["label"]
        for index in range(32)
    }
    # Assert
    assert first == {"TEST_GO_ONE", "TEST_GO_TWO"}


def test_same_agent_retains_deterministic_initial_affinity():
    # Arrange
    rows = [{"label": "TEST_GO_ONE"}, {"label": "TEST_GO_TWO"}]
    # Act
    initial = spread_declared_accounts("scitex-app", "opencode-go", rows)
    repeated = spread_declared_accounts("scitex-app", "opencode-go", rows)
    # Assert
    assert repeated == initial


def test_spreading_keeps_all_declared_credentials():
    # Arrange
    rows = [{"label": "TEST_GO_ONE"}, {"label": "TEST_GO_TWO"}]
    # Act
    labels = {
        row["label"] for row in spread_declared_accounts("app", "opencode-go", rows)
    }
    # Assert
    assert labels == {"TEST_GO_ONE", "TEST_GO_TWO"}


def test_restart_materialization_preserves_actual_cooldown(declared_pool, tmp_path):
    # Arrange
    config, rendered = declared_pool
    _, pools = configure_failover(config, rendered)
    materialize_pools(tmp_path, pools)
    auth = tmp_path / "auth.json"
    previous = json.loads(auth.read_text())
    previous["credential_pool"]["opencode-go"][0]["cooldown_until"] = 1900000000
    auth.write_text(json.dumps(previous))
    # Act
    materialize_pools(tmp_path, pools)
    current = json.loads(auth.read_text())
    # Assert
    assert current["credential_pool"]["opencode-go"][0]["cooldown_until"] == 1900000000


def test_exclusive_single_account_retires_old_pool_member(declared_pool, tmp_path):
    # Arrange
    config, rendered = declared_pool
    _, original = configure_failover(config, rendered)
    materialize_pools(tmp_path, original)
    config.hermes_failover.accounts["scitex-free"] = ["TEST_GO_TWO"]
    _, exclusive = configure_failover(config, rendered)
    # Act
    materialize_pools(tmp_path, exclusive)
    current = json.loads((tmp_path / "auth.json").read_text())
    # Assert
    assert [row["label"] for row in current["credential_pool"]["opencode-go"]] == [
        "TEST_GO_TWO"
    ]
