"""OpenCode Go is an ordinary, fail-closed Hermes endpoint declaration."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import check
from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._launch_plan import compile_launch_plan
from scitex_agent_container.config._provider_parse import parse_provider_value
from scitex_agent_container.config._provider_validation import validate_provider
from scitex_agent_container.runtimes import _hermes_profile
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError

EXAMPLE = (
    Path(__file__).parents[2]
    / "examples"
    / "providers"
    / "opencode-go-hermes.yaml"
)


def _example_spec() -> dict:
    """Load the portable v3 example, then expose its neutral launch shape."""
    config = load_config(EXAMPLE)
    provider = config.claude.provider
    return {
        "harness": config.harness,
        "launch_mode": config.runtime,
        "container": {"backend": "apptainer"},
        "engine": config.engine_key,
        "available_engines": {
            config.engine_key: {
                "model": config.model,
                "endpoints": {
                    "openai-chat-completions": {
                        "url": f"{provider.base_url}/chat/completions",
                        "auth": {
                            "kind": "bearer",
                            "env": provider.auth_token_env,
                        },
                        "extra_headers": provider.extra_headers,
                    }
                },
            }
        },
    }


def test_real_preflight_loads_example_and_fails_closed_without_key(
    env_save_restore, tmp_path
):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("OPENCODE_GO_API_KEY")

    # Act
    result = CliRunner().invoke(check, [str(EXAMPLE)])

    # Assert
    assert (
        result.exit_code,
        "Config validation failed" in result.output,
        "provider key:" in result.output,
        "OPENCODE_GO_API_KEY" in result.output,
        "start would also refuse this spec" in result.output,
    ) == (1, False, True, True, True)


def test_standalone_opencode_go_config_resolves_exact_backend_identity(
    env_save_restore,
):
    # Arrange
    env_save_restore.set("OPENCODE_GO_API_KEY", "secret-must-not-be-serialized")

    # Act
    config = load_config(EXAMPLE)
    plan = _hermes_profile._launch_plan(config, launch_mode="tui")
    rendered = compile_hermes_config(plan, workdir="/work")
    provider = rendered["providers"]["sac-opencode-go-deepseek-v4.1-flash"]

    # Assert
    assert (
        plan.engine.key,
        plan.engine.model_id,
        plan.endpoint.url,
        plan.endpoint.auth_env,
        plan.session_id,
        rendered["model"],
        provider["base_url"],
        provider["key_env"],
        provider["extra_headers"]["User-Agent"],
        provider["extra_headers"]["x-opencode-session"],
        rendered["fallback_providers"],
        "secret-must-not-be-serialized" in repr(rendered),
    ) == (
        "opencode-go-deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "https://opencode.ai/zen/go/v1/chat/completions",
        "OPENCODE_GO_API_KEY",
        "sac:providers",
        {
            "default": "deepseek-v4.1-flash",
            "provider": "custom:sac-opencode-go-deepseek-v4.1-flash",
            "api_mode": "chat_completions",
        },
        "https://opencode.ai/zen/go/v1",
        "OPENCODE_GO_API_KEY",
        "scitex-agent-container/hermes",
        "sac:providers",
        [],
        False,
    )


def test_opaque_non_uuid_session_header_is_stable_for_same_sac_conversation():
    """OpenCode documents stability, and Hermes documents an opaque value."""
    # Arrange
    raw = _example_spec()
    tui = compile_launch_plan(
        raw,
        agent_name="scitex-notification",
        session_id="sac:scitex-notification:conversation-42",
    )
    raw["launch_mode"] = "headless"
    headless = compile_launch_plan(
        raw,
        agent_name="scitex-notification",
        session_id="sac:scitex-notification:conversation-42",
    )

    # Act
    values = [
        compile_hermes_config(plan, workdir="/work")["providers"][
            "sac-opencode-go-deepseek-v4.1-flash"
        ]["extra_headers"]["x-opencode-session"]
        for plan in (tui, headless)
    ]

    # Assert
    assert values == [
        "sac:scitex-notification:conversation-42",
        "sac:scitex-notification:conversation-42",
    ]


def test_provider_extra_headers_reach_launch_plan_without_vendor_branch():
    # Arrange
    provider = parse_provider_value(
        {
            "base_url": "https://opencode.ai/zen/go",
            "auth_token_env": "OPENCODE_GO_API_KEY",
            "extra_headers": {"x-opencode-session": "${sac:session_id}"},
        }
    )
    config = AgentConfig(
        name="scitex-notification",
        harness="hermes",
        runtime="tui",
    )
    config.engine_key = "opencode-go-deepseek-v4.1-flash"
    config.model = "deepseek-v4.1-flash"
    config.claude.provider = provider

    # Act
    endpoint = _hermes_profile._launch_plan(config, launch_mode="tui").endpoint

    # Assert
    assert endpoint.extra_headers == (
        ("x-opencode-session", "${sac:session_id}"),
    )


def test_provider_header_validation_rejects_injection():
    # Arrange
    provider = {
        "base_url": "https://opencode.ai/zen/go",
        "auth_token_env": "OPENCODE_GO_API_KEY",
        "extra_headers": {"x-opencode-session": "conversation\nAuthorization: bad"},
    }

    # Act
    errors = validate_provider(provider)

    # Assert
    assert any("without newlines" in error for error in errors)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "proxy-authorization",
        "X-API-Key",
        "Cookie",
        "Set-Cookie",
        "X-Goog-Api-Key",
    ],
)
def test_provider_validation_rejects_credential_bearing_extra_header(name):
    # Arrange
    provider = {
        "base_url": "https://opencode.ai/zen/go",
        "auth_token_env": "OPENCODE_GO_API_KEY",
        "extra_headers": {name: "must-not-be-serialized"},
    }

    # Act
    errors = validate_provider(provider)

    # Assert
    assert any("credential header" in error for error in errors)


@pytest.mark.parametrize(
    "name", ["Authorization", "Proxy-Authorization", "X-API-Key", "Cookie"]
)
def test_launch_plan_rejects_credential_bearing_extra_header(name):
    # Arrange
    raw = _example_spec()
    headers = raw["available_engines"]["opencode-go-deepseek-v4.1-flash"][
        "endpoints"
    ]["openai-chat-completions"]["extra_headers"]
    headers[name] = "must-not-be-serialized"

    # Act
    def action():
        compile_launch_plan(raw, agent_name="scitex-notification")

    # Assert
    with pytest.raises(ValueError, match="credential header"):
        action()


def test_hermes_compiler_refuses_missing_durable_session_identity():
    # Arrange
    bound = compile_launch_plan(
        _example_spec(),
        agent_name="scitex-notification",
        session_id="sac:scitex-notification:conversation-42",
    )
    unbound = replace(bound, session_id=None)

    # Act
    def action():
        compile_hermes_config(unbound, workdir="/work")

    # Assert
    with pytest.raises(ValueError, match="session-bound launch plan"):
        action()


def test_launch_plan_refuses_missing_opencode_auth_env():
    # Arrange
    raw = _example_spec()
    del raw["available_engines"]["opencode-go-deepseek-v4.1-flash"][
        "endpoints"
    ]["openai-chat-completions"]["auth"]["env"]

    # Act
    def action():
        compile_launch_plan(raw, agent_name="scitex-notification")

    # Assert
    with pytest.raises(ValueError, match="auth.env must be a nonempty string"):
        action()


def test_opencode_provider_refuses_unresolved_key_without_fallback(
    env_save_restore, tmp_path
):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("OPENCODE_GO_API_KEY")
    config = AgentConfig(name="scitex-notification", harness="hermes")
    config.claude.provider = parse_provider_value(
        {
            "base_url": "https://opencode.ai/zen/go",
            "auth_token_env": "OPENCODE_GO_API_KEY",
        }
    )

    # Act
    def action():
        _hermes_profile.resolve_provider_api_key(config)

    # Assert
    with pytest.raises(ProviderEnvError, match="OPENCODE_GO_API_KEY"):
        action()
