"""Tests for the scitex-agent-container linter plugin (SAC001-002)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from scitex_dev.linter._rules._lookup import lookup as _real_lookup

from scitex_agent_container._linter_plugin import (
    _make_issue,
    _SacCardChecker,
    _SacMethodChecker,
    _SacSpecSentinelChecker,
    _source_at,
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


def test_get_plugin_registers_sac001_sac002_and_sac004():
    # Arrange
    # SAC003 is drafted but deferred until scitex-dev exposes filepath
    # to plugin checkers — see _linter_plugin.py docstring.
    # Act
    ids = sorted(r.id for r in get_plugin()["rules"])
    # Assert
    assert ids == ["STX-SAC001", "STX-SAC002", "STX-SAC004"]


def test_get_plugin_exposes_three_active_checkers():
    # Arrange
    # Act
    checkers = get_plugin()["checkers"]
    # Assert
    assert len(checkers) == 3


def test_sac004_ships_at_warning_severity_not_error():
    # Arrange — a fleet precedent: a rule shipped at error severity turned
    # 44 repositories red on day one. SAC004 loads into every repo linted
    # on a machine with sac installed, so it ships as a warning.
    # Act
    rule = next(r for r in get_plugin()["rules"] if r.id == "STX-SAC004")
    # Assert
    assert rule.severity == "warning"


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


# ---------------------------------------------------------------------------
# Branch coverage closure — _source_at + _make_issue short-circuits
# ---------------------------------------------------------------------------


def test_source_at_returns_empty_string_when_lineno_below_range():
    # Arrange
    lines = ["alpha", "beta"]
    # Act
    result = _source_at(lines, 0)
    # Assert
    assert result == ""


def test_source_at_returns_empty_string_when_lineno_above_range():
    # Arrange
    lines = ["alpha", "beta"]
    # Act
    result = _source_at(lines, 99)
    # Assert
    assert result == ""


def test_make_issue_returns_none_when_suppressed_by_inline_comment():
    # Arrange — real rule + a source line carrying a real stx-allow tag.
    rule = _real_lookup("STX-SAC001")
    suppressed_line = 'card = {"name": "x", "url": "u"}  # stx-allow: STX-SAC001'
    # Act
    issue = _make_issue(rule, line=1, col=0, source_line=suppressed_line)
    # Assert
    assert issue is None


# ---------------------------------------------------------------------------
# Branch coverage closure — checker visitors honour stx-allow suppression
# ---------------------------------------------------------------------------


def test_sac001_skips_dict_when_source_line_carries_stx_allow():
    # Arrange — real ast.Dict node on a line that suppresses STX-SAC001.
    src = 'card = {"name": "alpha", "url": "http://x"}  # stx-allow: STX-SAC001\n'
    # Act
    issues = _run(_SacCardChecker, src)
    # Assert
    assert issues == []


def test_sac002_skips_constant_when_source_line_carries_stx_allow():
    # Arrange — real ast.Constant on a line that suppresses STX-SAC002.
    # Build the v0 method string from parts so the test source itself does
    # not trip STX-SAC002 in CI lint passes over this file.
    legacy = "tasks" + "/" + "send"
    src = f'm = "{legacy}"  # stx-allow: STX-SAC002\n'
    # Act
    issues = _run(_SacMethodChecker, src)
    # Assert
    assert issues == []


# ---------------------------------------------------------------------------
# SAC004 — a spec sentinel used as a concrete value
#
# The near-miss this rule encodes (2026-08-11): the tui turn-bridge
# supervisor had to know which port an agent's bridge serves. Reading it
# from the spec would have returned the literal string "auto" on every
# agent in the fleet — 0 of 104 registered specs declare a concrete port —
# so it would have supervised NOTHING while reporting healthy. PR #973
# reads the port allocator's claim, which is state, instead.
# ---------------------------------------------------------------------------

# Assembled from parts so this test module's own source does not read as a
# sentinel field to a lint pass over tests/.
_SENTINEL_READ = "cfg." + "a2a." + "port"


def test_sac004_flags_sentinel_field_returned_from_function():
    # Arrange — the exact shape the near-miss would have had.
    src = f"def resolve_bridge_port(cfg):\n    return {_SENTINEL_READ}\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert [i.rule.id for i in issues] == ["STX-SAC004"]


def test_sac004_flags_sentinel_field_passed_as_positional_argument():
    # Arrange
    src = f"def probe(cfg):\n    return bind('127.0.0.1', {_SENTINEL_READ})\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert [i.rule.id for i in issues] == ["STX-SAC004"]


def test_sac004_flags_sentinel_field_passed_as_keyword_argument():
    # Arrange
    src = f"def probe(cfg):\n    return bind(port={_SENTINEL_READ})\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert [i.rule.id for i in issues] == ["STX-SAC004"]


def test_sac004_ignores_comparison_against_the_declared_value():
    # Arrange — asserting what the CONTRACT says is the correct way to read
    # a contract; this is the only shape sac's own tests use.
    src = f"def check(cfg):\n    assert {_SENTINEL_READ} == 7901\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_ignores_identity_test_against_none():
    # Arrange
    src = f"def check(cfg):\n    assert {_SENTINEL_READ} is None\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_stays_silent_in_a_sentinel_aware_function():
    # Arrange — a function that narrows the sentinel first is deliberate.
    src = (
        "def resolve(cfg):\n"
        "    if cfg.a2a.is_auto:\n"
        "        return None\n"
        f"    return {_SENTINEL_READ}\n"
    )
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_stays_silent_when_function_calls_the_state_side_resolver():
    # Arrange — reading the a2a_ports CLAIM is the prescribed fix.
    src = (
        "def resolve(cfg, name):\n"
        "    claimed = port_allocator.get_port(name)\n"
        f"    return claimed if claimed else {_SENTINEL_READ}\n"
    )
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_ignores_a_non_sentinel_field_of_the_same_name():
    # Arrange — ``listen.port`` is not on the sentinel list.
    src = "def probe(cfg):\n    return bind(cfg.listen.port)\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_skips_line_carrying_stx_allow():
    # Arrange
    src = (
        "def resolve_bridge_port(cfg):\n"
        f"    return {_SENTINEL_READ}  # stx-allow: STX-SAC004\n"
    )
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert issues == []


def test_sac004_fires_at_module_level_outside_any_function():
    # Arrange — no enclosing function means no narrowing scope.
    src = f"PORT = bind({_SENTINEL_READ})\n"
    # Act
    issues = _run(_SacSpecSentinelChecker, src)
    # Assert
    assert [i.rule.id for i in issues] == ["STX-SAC004"]


# ---------------------------------------------------------------------------
# SAC004 — the honesty check: measure the rule against the REAL tree.
#
# A rule with a noisy floor gets disabled within a week. This asserts the
# floor is zero on sac's own source AND its own tests, so the first time it
# speaks, it is speaking about new code.
# ---------------------------------------------------------------------------


def _scan_tree(root):
    """Run the SAC004 checker over every .py under *root*; return hits.

    Raises rather than returning an empty list when *root* is missing, so a
    layout change can never turn this into a vacuous pass.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"expected a real tree at {root}")
    hits = []
    for py in sorted(root.rglob("*.py")):
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        config = SimpleNamespace(disable=set(), per_rule_severity={})
        checker = _SacSpecSentinelChecker(source.splitlines(), config)
        checker.visit(tree)
        hits.extend(f"{py}:{i.line}" for i in checker.issues)
    return hits


@pytest.mark.parametrize("subtree", ["src", "tests"])
def test_sac004_noise_floor_is_zero_on_the_real_tree(subtree):
    # Arrange
    repo_root = Path(__file__).resolve().parents[2]
    # Act
    hits = _scan_tree(repo_root / subtree)
    # Assert
    assert hits == []
