"""Pure additive formatting/schema contracts preserve historical providers."""

import json

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_list_build import (
    build_provider_accounts_json,
)
from scitex_agent_container.cli_pkg._account_list_cmd import account_list
from scitex_agent_container.cli_pkg._account_provider_usage import render_provider_usage
from scitex_agent_container.config import load_config
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec


def row(provider="commandcode"):
    return {
        "provider": provider, "name": "fixture", "qualified_id": provider + ":fixture",
        "aliases": ["fixture"], "usage_state": "unknown", "fetchedAt": None,
        "windows": [], "balances": {}, "cost": None, "subscription_fee": None,
    }


def test_human_unknown_cost_is_explicit():
    # Arrange
    rows = [row()]
    # Act
    text = render_provider_usage(rows)
    # Assert
    assert "Measured cost: unknown" in text


def test_human_unknown_fee_is_explicit():
    # Arrange
    rows = [row()]
    # Act
    text = render_provider_usage(rows)
    # Assert
    assert "Subscription fee: unknown" in text


def test_human_old_snapshot_says_stale():
    # Arrange
    rows = [{**row(), "usage_state": "stale"}]
    # Act
    text = render_provider_usage(rows)
    # Assert
    assert "commandcode:fixture: stale" in text


def test_aliases_are_named_without_an_extra_usage_row():
    # Arrange
    rows = [{**row(), "aliases": ["fixture", "second"]}]
    # Act
    text = render_provider_usage(rows)
    # Assert
    assert "Shared credential aliases: fixture, second" in text


def test_api_alias_is_added_to_cross_provider_inventory():
    # Arrange
    usage = [row()]
    # Act
    entries = build_provider_accounts_json([], [], usage)
    # Assert
    assert entries[0]["qualified_id"] == "commandcode:fixture"


def test_native_usage_does_not_duplicate_existing_openai_account():
    # Arrange
    meta = [{"gateway_alias": "fixture", "auth_mode": "chatgpt"}]
    # Act
    entries = build_provider_accounts_json([], meta, [row("openai")])
    # Assert
    assert len(entries) == 1


def test_historical_claude_schema_is_preserved():
    # Arrange
    stored = [{"provider": "claude-code", "name": "existing", "freshness": "VALID"}]
    # Act
    entries = build_provider_accounts_json(stored, [], [row()])
    # Assert
    assert entries[0] == stored[0]


def test_two_openai_subscription_accounts_share_explicit_selection_contract():
    # Arrange
    meta = [{"gateway_alias": name, "auth_mode": "chatgpt"} for name in ("one", "two")]
    # Act
    entries = build_provider_accounts_json([], meta)
    # Assert
    assert [entry["selection"] for entry in entries] == [
        {"provider": "openai", "account": "openai:one"},
        {"provider": "openai", "account": "openai:two"},
    ]


def test_openai_rotation_is_truthfully_unimplemented():
    # Arrange
    meta = [{"gateway_alias": "one", "auth_mode": "chatgpt"}]
    # Act
    entries = build_provider_accounts_json([], meta)
    # Assert
    assert entries[0]["capabilities"]["automatic_rotation"] == "not-implemented"


@pytest.mark.parametrize("selected", ["one", "two"])
def test_existing_parser_selects_exact_subscription_without_auth_copy(tmp_path, selected):
    # Arrange
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump({
        "apiVersion": "scitex-agent-container/v3", "kind": "Agent",
        "spec": explicit_spec({
            "host": "host-fixture", "runtime": "tui", "harness": "codex",
            "workdir": str(tmp_path), "engine": selected,
            "available_engines": {
                name: {"harness": "codex", "model": "gpt-6.1-sol",
                       "subscription": {"provider": "openai", "account": "openai:" + name}}
                for name in ("one", "two")
            },
        }),
    }))
    # Act
    config = load_config(path)
    # Assert
    assert config.subscription_account == "openai:" + selected


@pytest.fixture
def passive_cli_inventory(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("CODEX_HOME")
    env_save_restore.delete("SCITEX_GENAI_CODEX_HOMES")
    env_save_restore.set("COMMANDCODE_API_KEY_01", "synthetic-same")
    env_save_restore.set("COMMANDCODE_API_KEY_ywatanabe", "synthetic-same")
    # Act
    result = CliRunner().invoke(account_list, [
        "--json", "--passive", "--no-fanout", "--no-refresh-quota",
    ])
    return json.loads(result.output)


def test_actual_json_cli_keeps_api_alias_without_network(passive_cli_inventory):
    # Arrange
    payload = passive_cli_inventory
    # Act
    provider = payload["provider_usage"][0]["provider"]
    # Assert
    assert provider == "commandcode"


def test_actual_json_cli_preserves_historical_top_level_keys(passive_cli_inventory):
    # Arrange
    payload = passive_cli_inventory
    # Act
    old_keys = {"active", "openai", "openai_accounts", "openai_error", "stored", "accounts", "hosts"}
    # Assert
    assert old_keys <= payload.keys()


def test_actual_json_cli_keeps_shared_aliases_once(passive_cli_inventory):
    # Arrange
    payload = passive_cli_inventory
    # Act
    rows = payload["provider_usage"]
    # Assert
    assert len(rows) == 1


def test_actual_json_cli_cannot_emit_key_value(passive_cli_inventory):
    # Arrange
    payload = passive_cli_inventory
    # Act
    output = json.dumps(payload)
    # Assert
    assert "synthetic-same" not in output
