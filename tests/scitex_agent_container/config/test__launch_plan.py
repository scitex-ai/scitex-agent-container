"""Selection cannot mutate the spec or bind the engine to a harness."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from scitex_agent_container.config._launch_plan import compile_launch_plan


def spec():
    return {
        "harness": "claude-code",
        "launch_mode": "headless",
        "container": {"backend": "apptainer"},
        "engine": "qwen",
        "engines": {
            "qwen": {
                "model": "qwen38-27b",
                "parameters": {"context_window_tokens": 1048576},
                "endpoints": {
                    "anthropic-messages": {
                        "url": "http://gateway/prefix/v1/messages",
                        "auth": {"kind": "api-key", "env": "KEY"},
                    },
                    "openai-responses": {
                        "url": "http://gateway/prefix/v1/responses",
                        "auth": {"kind": "bearer", "env": "KEY"},
                    },
                },
            }
        },
    }


def test_same_engine_across_harnesses_without_mutating_spec(monkeypatch):
    raw = spec()
    original = deepcopy(raw)
    monkeypatch.setenv("KEY", "secret-not-in-plan")
    claude = compile_launch_plan(raw)
    pi = compile_launch_plan(raw, harness="pi")
    codex = compile_launch_plan(raw, harness="codex")
    hermes = compile_launch_plan(raw, harness="hermes")
    assert claude.engine == pi.engine == codex.engine == hermes.engine
    assert raw == original
    assert "secret-not-in-plan" not in repr(pi)
    assert pi.endpoint.url == "http://gateway/prefix/v1/responses"
    assert hermes.endpoint.url == "http://gateway/prefix/v1/responses"
    with pytest.raises(FrozenInstanceError):
        pi.engine.model_id = "another-model"


def test_no_fleet_or_implicit_engine_fallback():
    with pytest.raises(ValueError, match="unknown engine"):
        compile_launch_plan(spec(), engine="missing")


def test_missing_protocol_refuses_pairing():
    raw = spec()
    del raw["engines"]["qwen"]["endpoints"]["openai-responses"]
    with pytest.raises(ValueError, match="requires one of"):
        compile_launch_plan(raw, harness="codex")


@pytest.mark.parametrize(
    "field,value", [("harness", "codex"), ("default", True), ("env", {"KEY": "secret"})]
)
def test_engine_cannot_own_other_axes(field, value):
    raw = spec()
    raw["engines"]["qwen"][field] = value
    with pytest.raises(ValueError, match="unknown fields"):
        compile_launch_plan(raw)


@pytest.mark.parametrize("value", [True, 0, -1, "1048576"])
def test_invalid_context_is_rejected(value):
    raw = spec()
    raw["engines"]["qwen"]["parameters"]["context_window_tokens"] = value
    with pytest.raises(ValueError, match="positive integer"):
        compile_launch_plan(raw)


def test_legacy_harness_alias_normalizes_at_boundary():
    assert compile_launch_plan(spec(), harness="anthropic").harness == "claude-code"


def test_invalid_inactive_engine_is_not_hidden():
    raw = spec()
    raw["engines"]["bad"] = {"model": "other"}
    with pytest.raises(ValueError, match="endpoints"):
        compile_launch_plan(raw)


def test_hermes_prefers_chat_completions_when_declared():
    raw = spec()
    raw["engines"]["qwen"]["endpoints"]["openai-chat-completions"] = {
        "url": "http://gateway/prefix/v1/chat/completions",
        "auth": {"kind": "bearer", "env": "KEY"},
    }

    plan = compile_launch_plan(raw, harness="hermes")

    assert plan.endpoint.protocol == "openai-chat-completions"
