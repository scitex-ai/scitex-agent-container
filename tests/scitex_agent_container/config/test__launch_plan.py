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


def test_same_engine_across_harnesses_without_mutating_spec(env_save_restore):
    # Arrange
    raw = spec()
    original = deepcopy(raw)
    env_save_restore.set("KEY", "secret-not-in-plan")
    # Act
    claude = compile_launch_plan(raw)
    pi = compile_launch_plan(raw, harness="pi")
    codex = compile_launch_plan(raw, harness="codex")
    hermes = compile_launch_plan(raw, harness="hermes")
    # Assert
    assert (
        claude.engine == pi.engine == codex.engine == hermes.engine
        and raw == original
        and "secret-not-in-plan" not in repr(pi)
        and pi.endpoint.url == "http://gateway/prefix/v1/responses"
        and hermes.endpoint.url == "http://gateway/prefix/v1/responses"
    )


def test_launch_plan_is_immutable():
    # Arrange
    plan = compile_launch_plan(spec(), harness="pi")
    # Act
    ctx = pytest.raises(FrozenInstanceError)
    # Assert
    with ctx:
        plan.engine.model_id = "another-model"


def test_agent_identity_is_explicit_and_immutable_in_launch_plan():
    # Arrange
    raw = spec()
    # Act
    plan = compile_launch_plan(raw, harness="hermes", agent_name="scitex-hub")
    # Assert
    assert plan.agent_name == "scitex-hub"


@pytest.mark.parametrize(
    "value", ["", "SCITEX-HUB", "scitex/hub", "scitex hub", "scitex\nhub"]
)
def test_invalid_agent_identity_is_rejected(value):
    # Arrange
    raw = spec()
    # Act
    ctx = pytest.raises(ValueError, match="agent_name")
    # Assert
    with ctx:
        compile_launch_plan(raw, harness="hermes", agent_name=value)


def test_no_fleet_or_implicit_engine_fallback():
    # Arrange
    raw = spec()
    # Act
    ctx = pytest.raises(ValueError, match="unknown engine")
    # Assert
    with ctx:
        compile_launch_plan(raw, engine="missing")


def test_explicit_harness_must_be_declared_available_without_fallback():
    raw = spec()
    raw["available_harnesses"] = {"claude-code": {}, "hermes": {}}

    with pytest.raises(ValueError) as caught:
        compile_launch_plan(raw, harness="codex")

    message = str(caught.value)
    assert (
        "available harnesses: claude-code, hermes" in message
        and "No fallback was selected" in message
    )


def test_incompatible_pair_feedback_lists_both_available_axes():
    raw = spec()
    raw["available_harnesses"] = {"claude-code": {}, "codex": {}}
    del raw["engines"]["qwen"]["endpoints"]["openai-responses"]

    with pytest.raises(ValueError) as caught:
        compile_launch_plan(raw, harness="codex", engine="qwen")

    message = str(caught.value)
    assert all(
        fragment in message
        for fragment in (
            "incompatible harness/engine pair",
            "available harnesses: claude-code, codex",
            "available engines: qwen",
            "No fallback was selected",
        )
    )


def test_missing_protocol_refuses_pairing():
    # Arrange
    raw = spec()
    del raw["engines"]["qwen"]["endpoints"]["openai-responses"]
    # Act
    ctx = pytest.raises(ValueError, match="requires one of")
    # Assert
    with ctx:
        compile_launch_plan(raw, harness="codex")


@pytest.mark.parametrize(
    "field,value", [("harness", "codex"), ("default", True), ("env", {"KEY": "secret"})]
)
def test_engine_cannot_own_other_axes(field, value):
    # Arrange
    raw = spec()
    raw["engines"]["qwen"][field] = value
    # Act
    ctx = pytest.raises(ValueError, match="unknown fields")
    # Assert
    with ctx:
        compile_launch_plan(raw)


@pytest.mark.parametrize("value", [True, 0, -1, "1048576"])
def test_invalid_context_is_rejected(value):
    # Arrange
    raw = spec()
    raw["engines"]["qwen"]["parameters"]["context_window_tokens"] = value
    # Act
    ctx = pytest.raises(ValueError, match="positive integer")
    # Assert
    with ctx:
        compile_launch_plan(raw)


@pytest.mark.parametrize(
    "timeouts",
    [
        {"upstream_deadline_seconds": 1800},
        {"client_abandonment_seconds": 1860},
        {
            "upstream_deadline_seconds": 1800,
            "client_abandonment_seconds": 1800,
        },
        {
            "upstream_deadline_seconds": 1800,
            "client_abandonment_seconds": 1799,
        },
        {
            "upstream_deadline_seconds": 1800,
            "client_abandonment_seconds": True,
        },
    ],
)
def test_invalid_timeout_contract_is_rejected(timeouts):
    # Arrange
    raw = spec()
    raw["engines"]["qwen"]["timeouts"] = timeouts

    # Act
    ctx = pytest.raises(ValueError, match="timeouts")

    # Assert
    with ctx:
        compile_launch_plan(raw)


def test_timeout_contract_is_explicit_in_launch_plan():
    # Arrange
    raw = spec()
    raw["engines"]["qwen"]["timeouts"] = {
        "upstream_deadline_seconds": 1800,
        "client_abandonment_seconds": 1860,
    }

    # Act
    engine = compile_launch_plan(raw).engine

    # Assert
    assert (
        engine.upstream_deadline_seconds,
        engine.client_abandonment_seconds,
    ) == (1800, 1860)


def test_legacy_harness_alias_normalizes_at_boundary():
    # Arrange
    raw = spec()
    # Act
    harness = compile_launch_plan(raw, harness="anthropic").harness
    # Assert
    assert harness == "claude-code"


def test_invalid_inactive_engine_is_not_hidden():
    # Arrange
    raw = spec()
    raw["engines"]["bad"] = {"model": "other"}
    # Act
    ctx = pytest.raises(ValueError, match="endpoints")
    # Assert
    with ctx:
        compile_launch_plan(raw)


def test_hermes_prefers_chat_completions_when_declared():
    # Arrange
    raw = spec()
    raw["engines"]["qwen"]["endpoints"]["openai-chat-completions"] = {
        "url": "http://gateway/prefix/v1/chat/completions",
        "auth": {"kind": "bearer", "env": "KEY"},
    }

    # Act
    plan = compile_launch_plan(raw, harness="hermes")
    # Assert
    assert plan.endpoint.protocol == "openai-chat-completions"


def test_delegation_policy_defaults_are_harness_neutral_and_bounded():
    # Arrange
    raw = spec()

    # Act
    claude = compile_launch_plan(raw)
    hermes = compile_launch_plan(raw, harness="hermes")

    # Assert
    assert (
        claude.may_spawn is hermes.may_spawn is True
        and claude.delegation == hermes.delegation
        and hermes.delegation.max_concurrent_children == 2
        and hermes.delegation.worktree_isolation is True
    )


def test_explicit_spawn_deny_and_delegation_cap_reach_plan():
    # Arrange
    raw = spec()
    raw["lineage"] = {"may_spawn": False}
    raw["delegation"] = {
        "max_concurrent_children": 4,
        "worktree_isolation": False,
    }

    # Act
    plan = compile_launch_plan(raw, harness="hermes")
    # Assert
    assert (
        plan.may_spawn,
        plan.delegation.max_concurrent_children,
        plan.delegation.worktree_isolation,
    ) == (False, 4, False)


@pytest.mark.parametrize("value", [True, 0, -1, 9, "2"])
def test_invalid_delegation_cap_is_rejected(value):
    # Arrange
    raw = spec()
    raw["delegation"] = {"max_concurrent_children": value}

    # Act
    ctx = pytest.raises(ValueError, match="integer between 1 and 8")
    # Assert
    with ctx:
        compile_launch_plan(raw, harness="hermes")


def test_non_boolean_spawn_permission_is_rejected():
    # Arrange
    raw = spec()
    raw["lineage"] = {"may_spawn": "false"}

    # Act
    ctx = pytest.raises(ValueError, match="must be a boolean")
    # Assert
    with ctx:
        compile_launch_plan(raw, harness="hermes")


def test_explicit_spawn_allow_is_preserved():
    # Arrange
    raw = spec()
    raw["lineage"] = {"may_spawn": True}

    # Act
    may_spawn = compile_launch_plan(raw, harness="hermes").may_spawn
    # Assert
    assert may_spawn is True
