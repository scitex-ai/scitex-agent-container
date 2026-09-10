"""Hermes receives a deterministic derived profile without credentials."""

from copy import deepcopy

import pytest

from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._launch_plan import compile_launch_plan


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


def test_compiles_observed_qwen_profile_without_reading_secret(monkeypatch):
    raw = _spec()
    original = deepcopy(raw)
    monkeypatch.setenv("QWEN_KEY", "must-not-appear")

    result = compile_hermes_config(
        compile_launch_plan(raw), workdir="/home/ywatanabe/proj/scitex-scholar"
    )

    assert raw == original
    assert result["model"] == {
        "default": "qwen38-27b",
        "provider": "sac-qwen",
        "api_mode": "chat_completions",
    }
    assert result["providers"]["sac-qwen"] == {
        "name": "SAC qwen",
        "base_url": "http://gateway/prefix/v1",
        "key_env": "QWEN_KEY",
        "transport": "chat_completions",
        "model": "qwen38-27b",
        "default_model": "qwen38-27b",
        "models": {"qwen38-27b": {"context_length": 1_000_000}},
    }
    assert result["fallback_providers"] == []
    assert result["agent"]["reasoning_effort"] == "low"
    assert result["agent"]["disabled_toolsets"] == []
    assert result["delegation"] == {
        "max_concurrent_children": 2,
        "max_spawn_depth": 1,
        "orchestrator_enabled": False,
        "worktree_isolation": True,
    }
    assert result["approvals"] == {"mode": "off"}
    assert result["compression"] == {
        "enabled": True,
        "threshold": 0.80,
        "target_ratio": 0.20,
        "tail_mode": "lean",
        "in_place": True,
    }
    assert result["display"]["busy_input_mode"] == "queue"
    assert "must-not-appear" not in repr(result)


def test_refuses_relative_workdir():
    with pytest.raises(ValueError, match="absolute"):
        compile_hermes_config(compile_launch_plan(_spec()), workdir="relative")


def test_refuses_non_hermes_plan():
    raw = _spec()
    raw["harness"] = "codex"
    raw["available_engines"]["qwen"]["endpoints"]["openai-responses"] = {
        "url": "http://gateway/prefix/v1/responses",
        "auth": {"kind": "bearer", "env": "QWEN_KEY"},
    }
    with pytest.raises(ValueError, match="received harness"):
        compile_hermes_config(compile_launch_plan(raw), workdir="/work")


def test_compiler_accepts_tui_launch_mode():
    raw = _spec()
    raw["launch_mode"] = "tui"
    result = compile_hermes_config(compile_launch_plan(raw), workdir="/work")
    assert result["terminal"]["cwd"] == "/work"


def test_compiler_accepts_explicit_autonomous_approval_mode():
    result = compile_hermes_config(
        compile_launch_plan(_spec()), workdir="/work", approval_mode="off"
    )
    assert result["approvals"] == {"mode": "off"}


def test_spawn_deny_removes_hermes_delegate_task_toolset():
    raw = _spec()
    raw["lineage"] = {"may_spawn": False}

    result = compile_hermes_config(
        compile_launch_plan(raw), workdir="/work"
    )

    assert result["agent"]["disabled_toolsets"] == ["delegation"]


def test_explicit_parallelism_is_emitted_without_nested_fanout():
    raw = _spec()
    raw["delegation"] = {
        "max_concurrent_children": 4,
        "worktree_isolation": False,
    }

    result = compile_hermes_config(
        compile_launch_plan(raw), workdir="/work"
    )

    assert result["delegation"] == {
        "max_concurrent_children": 4,
        "max_spawn_depth": 1,
        "orchestrator_enabled": False,
        "worktree_isolation": False,
    }
