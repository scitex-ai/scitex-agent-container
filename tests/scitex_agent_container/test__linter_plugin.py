"""Tests for the scitex-agent-container linter plugin (SAC001-003)."""

from __future__ import annotations

import ast

import pytest

from scitex_agent_container._linter_plugin import (
    _SacCardChecker,
    _SacEnvChecker,
    _SacMethodChecker,
    get_plugin,
)

# ---------------------------------------------------------------------------
# Plugin shape
# ---------------------------------------------------------------------------


def test_get_plugin_returns_canonical_keys():
    # Arrange
    expected = {"rules", "call_rules", "axes_hints", "checkers"}
    # Act
    plugin = get_plugin()
    # Assert
    assert set(plugin) == expected


def test_get_plugin_registers_sac001_sac002_sac003():
    # Arrange
    # Act
    ids = sorted(r.id for r in get_plugin()["rules"])
    # Assert
    assert ids == ["STX-SAC001", "STX-SAC002", "STX-SAC003"]


def test_get_plugin_exposes_three_checkers():
    # Arrange
    # Act
    checkers = get_plugin()["checkers"]
    # Assert
    assert len(checkers) == 3


# ---------------------------------------------------------------------------
# SAC001 — AgentCard v0 fields
# ---------------------------------------------------------------------------


def test_sac001_flags_dict_with_url_field():
    # Arrange
    src = 'card = {"name": "alpha", "url": "http://x"}\n'
    # Act
    diags = _SacCardChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert [d.rule_id for d in diags] == ["STX-SAC001"]


def test_sac001_flags_dict_with_authentication_field():
    # Arrange
    src = 'card = {"name": "alpha", "authentication": "bearer"}\n'
    # Act
    diags = _SacCardChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert len(diags) == 1


def test_sac001_flags_dict_with_state_transition_history_field():
    # Arrange
    src = 'card = {"name": "alpha", "stateTransitionHistory": True}\n'
    # Act
    diags = _SacCardChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert len(diags) == 1


def test_sac001_ignores_dict_without_name_marker():
    # Arrange — no "name" key means it isn't an AgentCard
    src = 'd = {"url": "http://x", "authentication": "none"}\n'
    # Act
    diags = _SacCardChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert diags == []


def test_sac001_ignores_v1_shaped_card():
    # Arrange
    src = """card = {
        "name": "alpha",
        "supportedInterfaces": [{"transport": "jsonrpc"}],
    }
    """
    # Act
    diags = _SacCardChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert diags == []


# ---------------------------------------------------------------------------
# SAC002 — A2A v0 method names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["tasks/send", "tasks/sendSubscribe"])
def test_sac002_flags_legacy_method_string(method):
    # Arrange
    src = f'm = "{method}"\n'
    # Act
    diags = _SacMethodChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert len(diags) == 1


@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage", "Get"])
def test_sac002_ignores_v1_method_string(method):
    # Arrange
    src = f'm = "{method}"\n'
    # Act
    diags = _SacMethodChecker("/x.py").visit(ast.parse(src))
    # Assert
    assert diags == []


# ---------------------------------------------------------------------------
# SAC003 — direct os.environ access to SAC_ / SCITEX_AGENT_CONTAINER_ keys
# ---------------------------------------------------------------------------


def test_sac003_flags_subscript_read_of_sac_key():
    # Arrange
    src = 'import os\nv = os.environ["SAC_HUB_URL"]\n'
    # Act
    diags = _SacEnvChecker("/src/foo.py").visit(ast.parse(src))
    # Assert
    assert len(diags) == 1


def test_sac003_flags_get_call_of_long_form_key():
    # Arrange
    src = 'import os\nv = os.environ.get("SCITEX_AGENT_CONTAINER_HUB_URL")\n'
    # Act
    diags = _SacEnvChecker("/src/foo.py").visit(ast.parse(src))
    # Assert
    assert len(diags) == 1


def test_sac003_ignores_non_sac_env_key():
    # Arrange
    src = 'import os\nv = os.environ["PATH"]\n'
    # Act
    diags = _SacEnvChecker("/src/foo.py").visit(ast.parse(src))
    # Assert
    assert diags == []


def test_sac003_exempts_env_module_itself():
    # Arrange
    src = 'import os\nv = os.environ["SAC_HUB_URL"]\n'
    # Act
    diags = _SacEnvChecker("/src/scitex_agent_container/_env.py").visit(ast.parse(src))
    # Assert
    assert diags == []


def test_sac003_exempts_tests_directory():
    # Arrange
    src = 'import os\nv = os.environ["SAC_HUB_URL"]\n'
    # Act
    diags = _SacEnvChecker("/proj/tests/test_foo.py").visit(ast.parse(src))
    # Assert
    assert diags == []
