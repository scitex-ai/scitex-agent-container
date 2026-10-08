"""The compiled profile balances only its distinct declared pool members."""

import json
import os
from contextlib import contextmanager

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_failover as failover
from scitex_agent_container.runtimes._apptainer_provider import resolve_provider_api_key
from scitex_agent_container.runtimes._hermes_profile import _launch_plan


@contextmanager
def _env(mapping, *, delete=()):
    """Set/unset process env without mocks, restoring every name after."""
    names = (*mapping, *delete)
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(mapping)
    for name in delete:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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


def _recovery_config(first, second):
    config = AgentConfig(name="recovery", harness="hermes", runtime="tui")
    config.workdir = "/work"
    config.engine_key = "muse"
    config.model = "muse-spark-1.3-contributor"
    config.claude.provider = ProviderSpec(
        hermes_provider="opencode-go", auth_token_env=first
    )
    config.hermes_failover = HermesFailoverSpec(accounts={"muse": [first, second]})
    return config


def test_missing_first_slot_launches_on_installed_second(tmp_path):
    # Arrange
    first, second = "SAC_RECOVERY_UNINSTALLED", "SAC_RECOVERY_INSTALLED"
    with _env({second: "synthetic-installed"}, delete=[first]):
        config = _recovery_config(first, second)
        rendered = compile_hermes_config(_launch_plan(config), workdir="/work")
        # Act
        primary = failover.resolve_primary_key(config, resolve_provider_api_key)
        _, pools = failover.configure_failover(config, rendered)
    # Assert
    assert (primary, [r["label"] for r in pools["opencode-go"].credentials]) == (
        "synthetic-installed",
        [second],
    )


def test_missing_first_slot_is_suppressed_not_dropped(tmp_path):
    # Arrange
    first, second = "SAC_RECOVERY_UNINSTALLED", "SAC_RECOVERY_INSTALLED"
    with _env({second: "synthetic-installed"}, delete=[first]):
        config = _recovery_config(first, second)
        rendered = compile_hermes_config(_launch_plan(config), workdir="/work")
        # Act
        _, pools = failover.configure_failover(config, rendered)
    # Assert
    assert "env:" + first in pools["opencode-go"].suppressed_sources


def test_newly_installed_first_slot_joins_pool_and_keeps_cooldown(tmp_path):
    # Arrange
    first, second = "SAC_RECOVERY_UNINSTALLED", "SAC_RECOVERY_INSTALLED"
    with _env({second: "synthetic-installed"}, delete=[first]):
        config = _recovery_config(first, second)
        rendered = compile_hermes_config(_launch_plan(config), workdir="/work")
        _, pools = failover.configure_failover(config, rendered)
        pools["opencode-go"].credentials[0]["exhausted_until"] = 2_000_000_000
        failover.materialize_pools(tmp_path, pools)
        # Act
        os.environ[first] = "synthetic-newly-installed"
        try:
            _, pools = failover.configure_failover(config, rendered)
            failover.materialize_pools(tmp_path, pools)
        finally:
            os.environ.pop(first, None)
        rows = json.loads((tmp_path / "auth.json").read_text())["credential_pool"][
            "opencode-go"
        ]
    # Assert
    assert ([row["label"] for row in rows], rows[1]["exhausted_until"]) == (
        [first, second],
        2_000_000_000,
    )


def test_recovery_leaves_declared_accounts_untouched(tmp_path):
    # Arrange
    first, second = "SAC_RECOVERY_UNINSTALLED", "SAC_RECOVERY_INSTALLED"
    with _env(
        {first: "synthetic-newly-installed", second: "synthetic-installed"}
    ):
        config = _recovery_config(first, second)
        rendered = compile_hermes_config(_launch_plan(config), workdir="/work")
        # Act
        _, _pools = failover.configure_failover(config, rendered)
    # Assert
    assert config.hermes_failover.accounts["muse"] == [first, second]


def _health_pools():
    row = {
        "id": "sac-old",
        "label": "KEY_A",
        "source": "manual:sac:KEY_A",
        "auth_type": "api_key",
        "access_token": "fake-old",
    }
    return {"provider": failover.DeclaredPool([row], [])}, row


def test_rejected_key_stays_declared_but_marked_dead(tmp_path):
    # Arrange
    pools, _row = _health_pools()
    failover.materialize_pools(tmp_path, pools)
    path = tmp_path / "auth.json"
    store = json.loads(path.read_text())
    store["credential_pool"]["provider"][0].update(
        last_status="exhausted", last_error_code=401, last_status_at=1
    )
    path.write_text(json.dumps(store))
    # Act
    failover.materialize_pools(tmp_path, pools)
    # Assert
    assert [(entry["id"], entry["last_status"]) for entry in _entries(tmp_path)] == [
        ("sac-old", "dead")
    ]


def _entries(state_dir):
    return json.loads((state_dir / "auth.json").read_text())["credential_pool"][
        "provider"
    ]


def test_replaced_key_returns_without_carrying_old_rejection(tmp_path):
    # Arrange
    pools, row = _health_pools()
    failover.materialize_pools(tmp_path, pools)
    path = tmp_path / "auth.json"
    store = json.loads(path.read_text())
    store["credential_pool"]["provider"][0].update(
        last_status="exhausted", last_error_code=401, last_status_at=1
    )
    path.write_text(json.dumps(store))
    failover.materialize_pools(tmp_path, pools)
    pools["provider"].credentials[0] = {
        **row,
        "id": "sac-new",
        "access_token": "fake-new",
    }
    # Act
    failover.materialize_pools(tmp_path, pools)
    # Assert
    assert (_entries(tmp_path)[0]["id"], "last_status" in _entries(tmp_path)[0]) == (
        "sac-new",
        False,
    )
