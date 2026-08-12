# -*- coding: utf-8 -*-
# File: tests/scitex_agent_container/test__claude_hooks_plugin.py
"""sac's ``scitex_dev.hooks`` DECLARATION.

The provider returns plain rows keyed by ``HookRule`` field names rather
than importing the type, so these tests need no scitex-dev at all — see
the module docstring for why that decoupling is deliberate. What is
asserted is the part that can rot on our side: the entry point stays
declared, the declared script path still resolves to a real executable
file, and every row carries the fields the aggregator constructs from.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from scitex_agent_container import _claude_hooks_plugin as plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

ENTRY_POINT_GROUP = "scitex_dev.hooks"

RULE_ID = "sac.no-raw-apptainer-build"

#: The field names scitex-dev's HookRule is constructed from.
REQUIRED_FIELDS = {
    "id",
    "owner",
    "rule",
    "reason",
    "event",
    "severity",
    "matches",
    "script",
}


def _raw_build_rule() -> dict:
    (rule,) = [r for r in plugin.provide_hooks() if r["id"] == RULE_ID]
    return rule


# ---------------------------------------------------------------------------
# The shipped asset
# ---------------------------------------------------------------------------


def test_declared_script_resolves_to_a_real_file():
    # Arrange
    package_root = Path(plugin.__file__).resolve().parent
    # Act
    script = package_root / plugin.DENY_RAW_APPTAINER_BUILD
    # Assert
    assert script.is_file()


def test_declared_script_is_executable():
    # Arrange
    package_root = Path(plugin.__file__).resolve().parent
    # Act
    script = package_root / plugin.DENY_RAW_APPTAINER_BUILD
    # Assert
    assert script.stat().st_mode & 0o111


def test_bundle_dir_matches_declared_relative_path():
    # Arrange
    package_root = Path(plugin.__file__).resolve().parent
    # Act
    from_relative = (package_root / plugin.DENY_RAW_APPTAINER_BUILD).parent
    # Assert
    assert from_relative == plugin.BUNDLE_DIR


# ---------------------------------------------------------------------------
# The federation wiring
# ---------------------------------------------------------------------------


def test_entry_point_group_is_declared_in_pyproject():
    # Arrange
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    # Act
    groups = data["project"]["entry-points"]
    # Assert
    assert ENTRY_POINT_GROUP in groups


def test_entry_point_targets_this_provider():
    # Arrange
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    # Act
    target = data["project"]["entry-points"][ENTRY_POINT_GROUP][
        "scitex-agent-container"
    ]
    # Assert
    assert target == (
        "scitex_agent_container._claude_hooks_plugin:provide_hooks"
    )


def test_provider_needs_no_scitex_dev_import():
    # Arrange — the decoupling this module exists to keep: declaring a row
    # must not make sac reference a peer module that may not be installed.
    source = Path(plugin.__file__).read_text(encoding="utf-8")
    # Act
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
        and "scitex_dev" in line
    ]
    # Assert
    assert import_lines == []


# ---------------------------------------------------------------------------
# The declared rows
# ---------------------------------------------------------------------------


def test_provider_yields_the_raw_build_rule():
    # Arrange
    expected = RULE_ID
    # Act
    ids = [rule["id"] for rule in plugin.provide_hooks()]
    # Assert
    assert expected in ids


def test_every_row_carries_the_required_fields():
    # Arrange
    rows = plugin.provide_hooks()
    # Act
    missing = {r["id"]: REQUIRED_FIELDS - set(r) for r in rows}
    # Assert
    assert missing == {r["id"]: set() for r in rows}


def test_raw_build_rule_denies_on_bash_pre_tool_use():
    # Arrange
    rule = _raw_build_rule()
    # Act
    shape = (rule["event"], rule["severity"], rule["matches"])
    # Assert
    assert shape == ("pre-tool-use", "deny", ("Bash",))


def test_raw_build_rule_points_at_the_shipped_script():
    # Arrange
    rule = _raw_build_rule()
    # Act
    script = Path(plugin.__file__).resolve().parent / str(rule["script"])
    # Assert
    assert script.is_file()


def test_every_rule_states_a_substantive_reason():
    # Arrange — the reason field is the auditable part; boilerplate defeats it
    rows = plugin.provide_hooks()
    # Act
    thin = [r["id"] for r in rows if len(str(r["reason"])) < 120]
    # Assert
    assert thin == []


def test_every_rule_is_owned_by_this_package():
    # Arrange
    rows = plugin.provide_hooks()
    # Act
    owners = {r["owner"] for r in rows}
    # Assert
    assert owners == {"scitex-agent-container"}

# EOF
