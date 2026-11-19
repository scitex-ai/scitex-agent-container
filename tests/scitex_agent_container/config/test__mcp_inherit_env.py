"""``spec.mcp_servers.<name>.inherit_env`` parsing and selection."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._mcp_inherit_env import (
    describe_mode,
    inherit_mode,
    parse_inherit_env,
    received_env_names,
    select_inherited,
    server_modes,
    without_sac_keys,
)

_ENV = {"SCITEX_STORE_DSN": "postgresql://h:1/db", "PGUSER": "u", "FOO": "bar"}


def test_entry_without_key_inherits_everything():
    # Arrange
    entry = {"command": "x"}
    # Act
    mode = inherit_mode("x", entry)
    # Assert
    assert mode is True


def test_false_mode_is_parsed_as_false():
    # Arrange
    value = False
    # Act
    mode = parse_inherit_env("x", value)
    # Assert
    assert mode is False


def test_name_list_becomes_frozen_allowlist():
    # Arrange
    value = ["PGUSER", " FOO "]
    # Act
    mode = parse_inherit_env("x", value)
    # Assert
    assert mode == frozenset({"PGUSER", "FOO"})


@pytest.mark.parametrize("bad", ["false", 0, 1, None, {"A": 1}, ["A", ""], ["A", 3]])
def test_invalid_inherit_env_values_are_refused(bad):
    # Arrange
    entry = {"command": "x", "inherit_env": bad}
    # Act
    ctx = pytest.raises(ValueError, match=r"spec\.mcp_servers\.srv\.inherit_env")
    # Assert
    with ctx:
        inherit_mode("srv", entry)


def test_false_mode_selects_no_inherited_env():
    # Arrange
    mode = False
    # Act
    selected = select_inherited(mode, _ENV)
    # Assert
    assert selected == {}


def test_true_mode_selects_all_inherited_env():
    # Arrange
    mode = True
    # Act
    selected = select_inherited(mode, _ENV)
    # Assert
    assert selected == _ENV


def test_allowlist_mode_selects_only_listed_names():
    # Arrange
    mode = frozenset({"PGUSER", "ABSENT"})
    # Act
    selected = select_inherited(mode, _ENV)
    # Assert
    assert selected == {"PGUSER": "u"}


def test_server_modes_cover_every_entry():
    # Arrange
    servers = {"a": {}, "b": {"inherit_env": False}, "c": {"inherit_env": ["X"]}}
    # Act
    modes = server_modes(servers)
    # Assert
    assert modes == {"a": True, "b": False, "c": frozenset({"X"})}


def test_sac_only_key_is_stripped_from_entry():
    # Arrange
    entry = {"command": "x", "env": {"A": "1"}, "inherit_env": False}
    # Act
    stripped = without_sac_keys(entry)
    # Assert
    assert stripped == {"command": "x", "env": {"A": "1"}}


def test_received_names_union_declared_and_inherited():
    # Arrange
    mode = frozenset({"PGUSER"})
    # Act
    names = received_env_names(mode, ["REPORT"], _ENV)
    # Assert
    assert names == ["PGUSER", "REPORT"]


def test_allowlist_mode_is_described_sorted():
    # Arrange
    mode = frozenset({"B", "A"})
    # Act
    label = describe_mode(mode)
    # Assert
    assert label == "[A, B]"
