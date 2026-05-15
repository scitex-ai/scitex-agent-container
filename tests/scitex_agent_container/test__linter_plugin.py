"""Tests for the scitex-agent-container linter plugin (SAC001-002)."""

from __future__ import annotations

import ast
from types import SimpleNamespace

import pytest

from scitex_agent_container._linter_plugin import (
    _SacCardChecker,
    _SacMethodChecker,
    get_plugin,
)


def _run(checker_cls, source: str):
    """Instantiate the checker matching scitex-dev's contract + run it."""
    config = SimpleNamespace(disable=set(), per_rule_severity={})
    lines = source.splitlines()
    checker = checker_cls(lines, config)
    checker.visit(ast.parse(source))
    return checker.issues


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


def test_get_plugin_registers_sac001_and_sac002():
    # Arrange
    # SAC003 is drafted but deferred until scitex-dev exposes filepath
    # to plugin checkers — see _linter_plugin.py docstring.
    # Act
    ids = sorted(r.id for r in get_plugin()["rules"])
    # Assert
    assert ids == ["STX-SAC001", "STX-SAC002"]


def test_get_plugin_exposes_two_active_checkers():
    # Arrange
    # Act
    checkers = get_plugin()["checkers"]
    # Assert
    assert len(checkers) == 2


# ---------------------------------------------------------------------------
# SAC001 — AgentCard v0 fields
# ---------------------------------------------------------------------------


def test_sac001_flags_dict_with_url_field():
    # Arrange
    src = 'card = {"name": "alpha", "url": "http://x"}\n'
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert [i.rule.id for i in issues] == ["STX-SAC001"]


def test_sac001_flags_dict_with_authentication_field():
    # Arrange
    src = 'card = {"name": "alpha", "authentication": "bearer"}\n'
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert len(issues) == 1


def test_sac001_flags_dict_with_state_transition_history_field():
    # Arrange
    src = 'card = {"name": "alpha", "stateTransitionHistory": True}\n'
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert len(issues) == 1


def test_sac001_ignores_dict_without_name_marker():
    # Arrange — no "name" key means it isn't an AgentCard
    src = 'd = {"url": "http://x", "authentication": "none"}\n'
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert issues == []


def test_sac001_ignores_v1_shaped_card():
    # Arrange
    src = (
        'card = {"name": "alpha", "supportedInterfaces": [{"transport": "jsonrpc"}]}\n'
    )
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert issues == []


# ---------------------------------------------------------------------------
# SAC002 — A2A v0 method names
# ---------------------------------------------------------------------------


# Legacy compat coverage: this is the linter plugin's own self-test that asserts
# detection of v0 method strings — the strings MUST appear here verbatim.
@pytest.mark.parametrize(
    "method",
    ["tasks/send", "tasks/sendSubscribe"],  # stx-allow: STX-SAC002
)
def test_sac002_flags_legacy_method_string(method):
    # Arrange
    src = f'm = "{method}"\n'
    # Act
    issues = _run(_SacMethodChecker, src)
    # Assert
    assert len(issues) == 1


@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage", "Get"])
def test_sac002_ignores_v1_method_string(method):
    # Arrange
    src = f'm = "{method}"\n'
    # Act
    issues = _run(_SacMethodChecker, src)
    # Assert
    assert issues == []


# SAC003 (direct os.environ read of SAC_* keys) is drafted but
# deferred — see _linter_plugin.py docstring. Tests will be added
# once scitex-dev's lint_source propagates filepath to plugin
# checkers; until then the rule would either misfire on legitimate
# cases (tests/, _env.py) or be inactive everywhere.
