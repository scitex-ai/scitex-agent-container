"""Hermes receives a deterministic derived profile without credentials."""

from copy import deepcopy

import pytest

from scitex_agent_container.config._hermes_compression import (
    HermesCompressionSpec,
    parse_selected_hermes_compression,
)
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._launch_plan import LaunchPlan, compile_launch_plan


def _spec() -> dict:
    return {
        "harness": "hermes",
        "launch_mode": "headless",
        "container": {"backend": "apptainer"},
        "engine": "qwen",
        "available_engines": {
            "qwen": {
                "model": "qwen38-27b",
                "parameters": {
                    "context_window_tokens": 1_000_000,
                    "reasoning_effort": "low",
                },
                "endpoints": {
                    "openai-chat-completions": {
                        "url": "http://gateway/prefix/v1/chat/completions",
                        "auth": {"kind": "bearer", "env": "QWEN_KEY"},
                    }
                },
            }
        },
    }


def _plan(raw: dict | None = None) -> LaunchPlan:
    return compile_launch_plan(
        _spec() if raw is None else raw,
        agent_name="scitex-scholar",
    )


def test_compiles_observed_qwen_profile_without_reading_secret(env_save_restore):
    # Arrange
    raw = _spec()
    original = deepcopy(raw)
    env_save_restore.set("QWEN_KEY", "must-not-appear")
    # Act
    result = compile_hermes_config(
        _plan(raw), workdir="/home/ywatanabe/proj/scitex-scholar"
    )
    # Assert
    observed = {
        "raw": raw,
        "model": result["model"],
        "provider": result["providers"]["sac-qwen"],
        "fallback_providers": result["fallback_providers"],
        "reasoning_effort": result["agent"]["reasoning_effort"],
        "disabled_toolsets": result["agent"]["disabled_toolsets"],
        "delegation": result["delegation"],
        "approvals": result["approvals"],
        "compression": result["compression"],
        "busy_input_mode": result["display"]["busy_input_mode"],
        "auxiliary": result["auxiliary"],
        "secret_absent": "must-not-appear" not in repr(result),
    }
    expected = {
        "raw": original,
        "model": {
            "default": "qwen38-27b",
            "provider": "sac-qwen",
            "api_mode": "chat_completions",
        },
        "provider": {
            "name": "SAC qwen",
            "base_url": "http://gateway/prefix/v1",
            "key_env": "QWEN_KEY",
            "transport": "chat_completions",
            "model": "qwen38-27b",
            "default_model": "qwen38-27b",
            "models": {"qwen38-27b": {"context_length": 1_000_000}},
            "extra_headers": {
                "X-SciTeX-Agent-ID": "scitex-scholar",
                "X-SciTeX-Session-ID": "sac:scitex-scholar",
            },
        },
        "fallback_providers": [],
        "reasoning_effort": "low",
        "disabled_toolsets": [],
        "delegation": {
            "max_concurrent_children": 2,
            "max_spawn_depth": 1,
            "orchestrator_enabled": False,
            "worktree_isolation": True,
        },
        "approvals": {"mode": "off"},
        "compression": {
            "enabled": True,
            "threshold": 0.80,
            "target_ratio": 0.20,
            "tail_mode": "lean",
            "in_place": True,
        },
        "busy_input_mode": "steer",
        "auxiliary": {
            "title_generation": {"enabled": False},
            "background_review": {"enabled": False},
        },
        "secret_absent": True,
    }
    assert observed == expected


def test_compiles_explicit_hermes_compression_controls():
    # Arrange
    compression = HermesCompressionSpec(
        threshold=0.85,
        threshold_tokens=524_288,
        target_ratio=0.30,
        tail_mode="legacy",
        in_place=False,
    )
    # Act
    result = compile_hermes_config(
        _plan(), workdir="/work", compression=compression
    )
    # Assert
    assert result["compression"] == {
        "enabled": True,
        "threshold": 0.85,
        "threshold_tokens": 524_288,
        "target_ratio": 0.30,
        "tail_mode": "legacy",
        "in_place": False,
    }


def test_parses_absolute_hermes_compression_threshold():
    # Arrange
    raw = _spec()
    raw["available_harnesses"] = {
        "hermes": {"compression": {"threshold_tokens": 262_144}}
    }
    # Act
    result = parse_selected_hermes_compression(raw)
    # Assert
    assert result.threshold_tokens == 262_144


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_refuses_invalid_absolute_hermes_compression_threshold(value):
    # Arrange
    def action():
        HermesCompressionSpec(threshold_tokens=value)

    # Act
    run = action
    # Assert
    with pytest.raises(ValueError, match="positive integer"):
        run()


def test_refuses_relative_workdir():
    # Arrange
    plan = _plan()
    # Act
    ctx = pytest.raises(ValueError, match="absolute")
    # Assert
    with ctx:
        compile_hermes_config(plan, workdir="relative")


def test_refuses_identity_free_plan_instead_of_emitting_unknown_headers():
    # Arrange
    plan = compile_launch_plan(_spec())
    # Act
    ctx = pytest.raises(ValueError, match="agent-bound launch plan")
    # Assert
    with ctx:
        compile_hermes_config(plan, workdir="/work")


def test_gateway_identity_is_stable_across_launch_modes_for_resume():
    # Arrange
    raw = _spec()
    headless = compile_launch_plan(raw, agent_name="scitex-scholar")
    raw["launch_mode"] = "tui"
    tui = compile_launch_plan(raw, agent_name="scitex-scholar")
    # Act
    headless_headers = compile_hermes_config(headless, workdir="/work")[
        "providers"
    ]["sac-qwen"]["extra_headers"]
    tui_headers = compile_hermes_config(tui, workdir="/work")["providers"][
        "sac-qwen"
    ]["extra_headers"]
    # Assert
    assert headless_headers == tui_headers == {
        "X-SciTeX-Agent-ID": "scitex-scholar",
        "X-SciTeX-Session-ID": "sac:scitex-scholar",
    }


def test_refuses_non_hermes_plan():
    # Arrange
    raw = _spec()
    raw["harness"] = "codex"
    raw["available_engines"]["qwen"]["endpoints"]["openai-responses"] = {
        "url": "http://gateway/prefix/v1/responses",
        "auth": {"kind": "bearer", "env": "QWEN_KEY"},
    }
    plan = _plan(raw)
    # Act
    ctx = pytest.raises(ValueError, match="received harness")
    # Assert
    with ctx:
        compile_hermes_config(plan, workdir="/work")


def test_compiler_accepts_tui_launch_mode():
    # Arrange
    raw = _spec()
    raw["launch_mode"] = "tui"
    # Act
    result = compile_hermes_config(_plan(raw), workdir="/work")
    # Assert
    assert result["terminal"]["cwd"] == "/work"


def test_compiler_accepts_explicit_autonomous_approval_mode():
    # Arrange
    plan = _plan()
    # Act
    result = compile_hermes_config(plan, workdir="/work", approval_mode="off")
    # Assert
    assert result["approvals"] == {"mode": "off"}


def test_long_inference_timeout_reaches_both_hermes_watchdogs():
    # Arrange
    raw = _spec()
    raw["available_engines"]["qwen"]["timeouts"] = {
        "upstream_deadline_seconds": 1800,
        "client_abandonment_seconds": 1860,
    }

    # Act
    result = compile_hermes_config(_plan(raw), workdir="/work")

    # Assert
    model = result["providers"]["sac-qwen"]["models"]["qwen38-27b"]
    assert model["timeout_seconds"] == model["stale_timeout_seconds"] == 1860


def test_spawn_deny_removes_hermes_delegate_task_toolset():
    # Arrange
    raw = _spec()
    raw["lineage"] = {"may_spawn": False}

    # Act
    result = compile_hermes_config(_plan(raw), workdir="/work")
    # Assert
    assert result["agent"]["disabled_toolsets"] == ["delegation"]


def test_explicit_parallelism_is_emitted_without_nested_fanout():
    # Arrange
    raw = _spec()
    raw["delegation"] = {
        "max_concurrent_children": 4,
        "worktree_isolation": False,
    }

    # Act
    result = compile_hermes_config(_plan(raw), workdir="/work")
    # Assert
    assert result["delegation"] == {
        "max_concurrent_children": 4,
        "max_spawn_depth": 1,
        "orchestrator_enabled": False,
        "worktree_isolation": False,
    }


def test_explicit_background_review_is_emitted_to_hermes_auxiliary_config():
    # Arrange
    plan = _plan()
    # Act
    result = compile_hermes_config(plan, workdir="/work", background_review=True)
    # Assert
    assert result["auxiliary"]["background_review"] == {"enabled": True}


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_background_review_refuses_non_boolean_values(value):
    # Arrange
    plan = _plan()
    # Act
    ctx = pytest.raises(ValueError, match="background_review must be a boolean")
    # Assert
    with ctx:
        compile_hermes_config(plan, workdir="/work", background_review=value)
