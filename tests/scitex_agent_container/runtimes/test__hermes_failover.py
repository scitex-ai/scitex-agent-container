"""The compiled profile balances only its distinct declared pool members."""

import json
import os

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_failover as failover
from scitex_agent_container.runtimes._apptainer_provider import resolve_provider_api_key
from scitex_agent_container.runtimes._hermes_profile import _launch_plan


@pytest.fixture
def synthetic_accounts():
    tokens = {
        "GO_ADMITTED_A": "synthetic-a",
        "GO_ADMITTED_B": "synthetic-b",
        "GO_ALIAS_A": "synthetic-a",
        "OPENCODE_GO_API_KEY_1": "synthetic-unadmitted",
    }
    name = "OPENCODE_GO_API_KEY_1"
    previous = os.environ.get(name)
    os.environ[name] = tokens[name]
    yield tokens
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _compile(tokens):
    config = AgentConfig(name="future", harness="hermes", runtime="tui")
    config.workdir = "/work"
    config.engine_key = "muse"
    config.model = "muse-spark-1.3-contributor"
    config.claude.provider = ProviderSpec(
        hermes_provider="opencode-go", auth_token_env="OPENCODE_GO_API_KEY"
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={"muse": ["GO_ADMITTED_A", "GO_ADMITTED_B", "GO_ALIAS_A"]},
        strategy="round_robin",
    )
    rendered = compile_hermes_config(_launch_plan(config), workdir="/work")
    _, pools = failover.configure_failover(
        config,
        rendered,
        credential_resolver=lambda c: tokens[c.claude.provider.auth_token_env],
    )
    return rendered, pools


def test_profile_forwards_the_declared_balance_strategy(synthetic_accounts):
    # Arrange
    compile_profile = _compile

    # Act
    rendered, _ = compile_profile(synthetic_accounts)

    # Assert
    assert rendered["credential_pool_strategies"] == {"opencode-go": "round_robin"}


def test_aliases_of_one_credential_do_not_receive_extra_weight(synthetic_accounts):
    # Arrange
    compile_profile = _compile

    # Act
    _, pools = compile_profile(synthetic_accounts)

    # Assert
    assert [row["label"] for row in pools["opencode-go"].credentials] == [
        "GO_ADMITTED_A",
        "GO_ADMITTED_B",
    ]


def test_account_balance_does_not_add_a_model_or_provider_backup(synthetic_accounts):
    # Arrange
    compile_profile = _compile

    # Act
    rendered, _ = compile_profile(synthetic_accounts)

    # Assert
    assert rendered["fallback_providers"] == []


def test_unlisted_ambient_slot_is_suppressed(synthetic_accounts):
    # Arrange
    compile_profile = _compile

    # Act
    _, pools = compile_profile(synthetic_accounts)

    # Assert
    assert "env:OPENCODE_GO_API_KEY_1" in pools["opencode-go"].suppressed_sources


def test_profile_refresh_preserves_existing_account_cooldown(
    tmp_path, synthetic_accounts
):
    # Arrange
    _, pools = _compile(synthetic_accounts)
    previous = dict(pools["opencode-go"].credentials[0], exhausted_until=2_000_000_000)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"credential_pool": {"opencode-go": [previous]}}))

    # Act
    failover.materialize_pools(tmp_path, pools)

    # Assert
    assert (
        json.loads(auth.read_text())["credential_pool"]["opencode-go"][0][
            "exhausted_until"
        ]
        == 2_000_000_000
    )


def test_missing_first_slot_can_launch_and_becomes_available_after_install(
    tmp_path, monkeypatch
):
    config = AgentConfig(name="recovery", harness="hermes", runtime="tui")
    config.workdir = "/work"
    config.engine_key = "muse"
    config.model = "muse-spark-1.3-contributor"
    first, second = "SAC_RECOVERY_UNINSTALLED", "SAC_RECOVERY_INSTALLED"
    monkeypatch.delenv(first, raising=False)
    monkeypatch.setenv(second, "synthetic-installed")
    config.claude.provider = ProviderSpec(
        hermes_provider="opencode-go", auth_token_env=first
    )
    config.hermes_failover = HermesFailoverSpec(accounts={"muse": [first, second]})
    rendered = compile_hermes_config(_launch_plan(config), workdir="/work")

    assert (
        failover.resolve_primary_key(config, resolve_provider_api_key)
        == "synthetic-installed"
    )
    _, pools = failover.configure_failover(config, rendered)
    assert [r["label"] for r in pools["opencode-go"].credentials] == [second]
    assert "env:" + first in pools["opencode-go"].suppressed_sources
    pools["opencode-go"].credentials[0]["exhausted_until"] = 2_000_000_000
    failover.materialize_pools(tmp_path, pools)

    monkeypatch.setenv(first, "synthetic-newly-installed")
    _, pools = failover.configure_failover(config, rendered)
    failover.materialize_pools(tmp_path, pools)
    rows = json.loads((tmp_path / "auth.json").read_text())["credential_pool"][
        "opencode-go"
    ]
    assert [row["label"] for row in rows] == [first, second]
    assert rows[1]["exhausted_until"] == 2_000_000_000
    assert config.hermes_failover.accounts["muse"] == [first, second]
