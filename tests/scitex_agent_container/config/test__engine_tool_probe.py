"""Exact tool-call probing stays bounded, native, and credential-free."""

from __future__ import annotations

import json

import pytest

from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._engine_tool_probe import (
    build_probe_plan,
    build_tool_probe_request,
    probe_engine_tools,
    validate_tool_probe_response,
)


def _config(harness: str = "codex") -> AgentConfig:
    config = AgentConfig(name="probe-agent", harness=harness, runtime="headless")
    config.engine_key = "qwen"
    config.model = "qwen38-27b"
    config.claude.model = "qwen38-27b"
    config.claude.provider = ProviderSpec(
        base_url="http://gateway/prefix",
        auth_token_env="QWEN_KEY",
    )
    return config


def test_current_codex_config_builds_responses_probe_plan() -> None:
    # Arrange
    config = _config()
    # Act
    plan = build_probe_plan(config)
    # Assert
    assert (
        plan.engine.key,
        plan.endpoint.protocol,
        plan.endpoint.url,
        plan.endpoint.auth_env,
    ) == (
        "qwen",
        "openai-responses",
        "http://gateway/prefix/v1/responses",
        "QWEN_KEY",
    )


def test_claude_code_alias_builds_native_messages_request() -> None:
    # Arrange
    plan = build_probe_plan(_config("anthropic"), harness="claude-code")
    # Act
    request = build_tool_probe_request(plan, "n")
    # Assert
    assert request["tool_choice"] == {"type": "tool", "name": "sac_engine_probe"}
    assert plan.endpoint.url == "http://gateway/prefix/v1/messages"


def test_responses_probe_requires_exact_call() -> None:
    # Arrange
    body = {
        "output": [
            {
                "type": "function_call",
                "name": "sac_engine_probe",
                "arguments": json.dumps({"nonce": "n"}),
            }
        ]
    }
    # Act
    exact = validate_tool_probe_response("openai-responses", body, "n")
    body["output"][0]["name"] = "sac_engine_probe\n<parameter=nonce"
    inexact = validate_tool_probe_response("openai-responses", body, "n")
    # Assert
    assert exact.ok is True
    assert (inexact.ok, inexact.diagnostic) == (False, "tool name does not match")


def test_responses_probe_rejects_duplicate_and_malformed_calls() -> None:
    # Arrange
    call = {
        "type": "function_call",
        "name": "sac_engine_probe",
        "arguments": "not-json",
    }
    # Act
    malformed = validate_tool_probe_response(
        "openai-responses", {"output": [call]}, "n"
    )
    duplicate = validate_tool_probe_response(
        "openai-responses", {"output": [call, call]}, "n"
    )
    # Assert
    assert malformed.diagnostic == "function arguments are not JSON"
    assert duplicate.diagnostic == "expected one function call, received 2"


def test_probe_sends_auth_without_returning_it() -> None:
    # Arrange
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "sac_engine_probe",
                            "arguments": json.dumps({"nonce": "fixed"}),
                        }
                    ]
                }
            ).encode()

    seen = {}

    def opener(request, timeout):
        seen["authorization"] = request.headers["Authorization"]
        seen["timeout"] = timeout
        return Response()

    # Act
    result = probe_engine_tools(
        build_probe_plan(_config()),
        key="secret",
        nonce="fixed",
        timeout_s=7,
        opener=opener,
    )
    # Assert
    assert result.ok is True
    assert seen == {"authorization": "Bearer secret", "timeout": 7}
    assert "secret" not in repr(result)


def test_unsupported_harness_refuses_instead_of_guessing_a_protocol() -> None:
    # Arrange
    config = _config("hermes")

    # Act
    def action():
        return build_probe_plan(config)

    # Assert
    with pytest.raises(ValueError, match="does not support harness 'hermes'"):
        action()
