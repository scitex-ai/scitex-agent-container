"""Tests for ``scitex_agent_container.a2a._build`` — yaml + executor builder.

Pure-function helpers, so each test uses a real on-disk yaml path
(via ``tmp_path``) or a literal dict — no mocks/monkeypatch. The
gap before these tests was lines 36-44 (spec.yaml dir-as-SSoT branch
+ ValueError), 53 (handler default fallback), 67/73 (permission_mode
explicit + flags branches), and 92 (build_executor unknown handler).
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.a2a._build import (
    agent_name_from_yaml,
    build_executor,
    select_handler_key,
    select_permission_mode,
)


def test_agent_name_from_yaml_uses_parent_dir_for_spec_file(tmp_path: Path):
    # Arrange — dir-as-SSoT layout: agents/<name>/spec.yaml
    agent_dir = tmp_path / "my_agent"
    agent_dir.mkdir()
    spec_path = agent_dir / "spec.yaml"
    spec_path.write_text("metadata: {}\n")
    # Act
    name = agent_name_from_yaml(spec_path, {"metadata": {}})
    # Assert
    assert name == "my_agent"


def test_agent_name_from_yaml_falls_back_to_file_stem(tmp_path: Path):
    # Arrange — non-spec filename, no metadata.name
    path = tmp_path / "alpha.yaml"
    path.write_text("metadata: {}\n")
    # Act
    name = agent_name_from_yaml(path, {})
    # Assert
    assert name == "alpha"


def test_agent_name_from_yaml_raises_for_spec_with_empty_parent():
    # Arrange — pathological path: 'spec.yaml' with no parent dir name
    path = Path("spec.yaml")
    # Act
    caught: Exception | None = None
    try:
        agent_name_from_yaml(path, {})
    except ValueError as exc:
        caught = exc
    # Assert
    assert isinstance(caught, ValueError)


def test_select_handler_key_falls_back_to_default():
    # Arrange — yaml without spec.a2a.handler
    v3: dict = {}
    # Act
    key = select_handler_key(v3, default="echo")
    # Assert
    assert key == "echo"


def test_select_permission_mode_returns_explicit_string():
    # Arrange
    claude_block = {"permission_mode": "bypassPermissions"}
    # Act
    mode = select_permission_mode(claude_block)
    # Assert
    assert mode == "bypassPermissions"


def test_select_permission_mode_maps_dangerous_flag_to_bypass():
    # Arrange — legacy form: explicit flag in claude.flags list
    claude_block = {"flags": ["--dangerously-skip-permissions"]}
    # Act
    mode = select_permission_mode(claude_block)
    # Assert
    assert mode == "bypassPermissions"


def test_build_executor_raises_on_unknown_handler():
    # Arrange
    v3 = {"spec": {"claude": {}}}
    # Act
    caught: Exception | None = None
    try:
        build_executor(
            name="agentX",
            handler_key="not_a_real_handler",
            v3=v3,
            a2a_port=None,
        )
    except ValueError as exc:
        caught = exc
    # Assert
    assert isinstance(caught, ValueError)
