"""Tests for ``runtimes/_provider_common.py`` (openai-compat-1 extraction).

These exercise the module DIRECTLY (not via the ``_sdk_common`` re-export)
to prove the split is standalone-importable and behaviorally unchanged —
the openai-compat-2 runner will import this module on its own, without
pulling in ``_sdk_common``'s Anthropic-specific auth/options code.

``test__sdk_common.py`` already covers ``resolve_agent_workspace`` /
``project_runtime_root`` end-to-end through the ``_sdk_common`` re-export
(regression coverage for the extraction); this file adds direct-import
coverage plus the ``project_runtime_root`` no-config-path case, which had
no prior test anywhere in the suite.

PA-306: no ``monkeypatch`` — env/attribute mutations use an explicit
save/restore fixture, matching ``test__sdk_common.py``'s ``_Env`` pattern.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scitex_agent_container.runtimes import _provider_common
from scitex_agent_container.runtimes._provider_common import (
    project_runtime_root,
    resolve_agent_workspace,
)


class _Env:
    """Records attribute mutations and reverses them on teardown."""

    def __init__(self) -> None:
        self._attr_snapshots: list[tuple[Any, str, Any]] = []

    def setattr_module(self, obj: Any, name: str, value: Any) -> None:
        if not any(a is obj and n == name for a, n, _ in self._attr_snapshots):
            self._attr_snapshots.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, prev in self._attr_snapshots:
            setattr(obj, name, prev)


@pytest.fixture
def env():
    e = _Env()
    try:
        yield e
    finally:
        e.restore()


def _swap_registry(env: _Env, entry: Any) -> None:
    import scitex_agent_container._state.registry as reg_mod

    class _FakeRegistry:
        def get(self, _name):
            return entry

    env.setattr_module(reg_mod, "Registry", _FakeRegistry)


def _swap_load_config(env: _Env, workdir: str) -> None:
    import scitex_agent_container.config as cfg_mod

    env.setattr_module(
        cfg_mod, "load_config", lambda _path: SimpleNamespace(expanded_workdir=workdir)
    )


# ---------------------------------------------------------------------------
# resolve_agent_workspace — direct import, standalone behavior
# ---------------------------------------------------------------------------


def test_resolve_agent_workspace_unknown_agent_returns_empty(env: _Env):
    # Arrange
    _swap_registry(env, None)
    # Act
    result = resolve_agent_workspace("nope")
    # Assert
    assert result == ({}, None)


def test_resolve_agent_workspace_returns_cwd_without_mcp_json(
    env: _Env, tmp_path: Path
):
    # Arrange
    ws = tmp_path / "ws"
    ws.mkdir()
    _swap_registry(env, {"config": "cfg.yaml"})
    _swap_load_config(env, str(ws))
    # Act
    result = resolve_agent_workspace("alpha")
    # Assert
    assert result == ({}, str(ws))


def test_resolve_agent_workspace_substitutes_env_var(env: _Env, tmp_path: Path):
    # Arrange
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"stx": {"command": "scitex", "args": ["${MY_TOKEN}"]}}}
        )
    )
    import os as _os

    prev = _os.environ.get("MY_TOKEN")
    _os.environ["MY_TOKEN"] = "tk-123"
    try:
        _swap_registry(env, {"config": "cfg.yaml"})
        _swap_load_config(env, str(ws))
        # Act
        servers, _cwd = resolve_agent_workspace("alpha")
    finally:
        if prev is None:
            _os.environ.pop("MY_TOKEN", None)
        else:
            _os.environ["MY_TOKEN"] = prev
    # Assert
    assert servers["stx"]["args"] == ["tk-123"]


def test_resolve_agent_workspace_defaults_type_to_stdio(env: _Env, tmp_path: Path):
    # Arrange
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"stx": {"command": "scitex"}}})
    )
    _swap_registry(env, {"config": "cfg.yaml"})
    _swap_load_config(env, str(ws))
    # Act
    servers, _cwd = resolve_agent_workspace("alpha")
    # Assert
    assert servers["stx"]["type"] == "stdio"


# ---------------------------------------------------------------------------
# project_runtime_root
# ---------------------------------------------------------------------------


def test_project_runtime_root_returns_none_without_config_path():
    # Arrange
    config = SimpleNamespace(config_path="")
    # Act
    result = project_runtime_root(config)
    # Assert
    assert result is None


def test_project_runtime_root_returns_none_for_unscoped_path(tmp_path: Path):
    # Arrange — a tmp dir with no ``.scitex/agent-container`` marker
    # anywhere in its ancestry.
    config = SimpleNamespace(config_path=str(tmp_path / "spec.yaml"))
    # Act
    result = project_runtime_root(config)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Module surface — the pieces openai-compat-2 will import directly
# ---------------------------------------------------------------------------


def test_module_all_exports_project_runtime_root_and_resolve_workspace():
    # Arrange
    # Act
    exported = set(_provider_common.__all__)
    # Assert
    assert exported == {"project_runtime_root", "resolve_agent_workspace"}
