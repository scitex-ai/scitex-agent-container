# -*- coding: utf-8 -*-
# File: tests/scitex_agent_container/test__claude_hooks_plugin.py
"""sac's ``scitex_dev.hooks`` DECLARATION.

The rows themselves cannot be constructed until scitex-dev ships the
``scitex_dev.hooks`` contract, so those cases skip cleanly. What is asserted
unconditionally is the part that can rot silently on our side: the
entry point stays declared, the declared script path still resolves to a
real file, and the provider stays importable without scitex-dev present
(the lazy-import contract that lets this entry point ship inert).
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from scitex_agent_container import _claude_hooks_plugin as plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

ENTRY_POINT_GROUP = "scitex_dev.hooks"

_HAS_CONTRACT = importlib.util.find_spec("scitex_dev.hooks") is not None
_needs_contract = pytest.mark.skipif(
    not _HAS_CONTRACT,
    reason="scitex_dev.hooks contract not installed yet; provider ships inert",
)


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


def test_provider_imports_without_scitex_dev_contract():
    # Arrange — the lazy-import contract: declaring the entry point must not
    # require the contract to exist, or a scitex-dev lagging on PyPI breaks
    # import-time metadata for the whole distribution.
    module = plugin
    # Act
    provider = getattr(module, "provide_hooks", None)
    # Assert
    assert callable(provider)


@_needs_contract
def test_provider_yields_the_raw_build_rule():
    # Arrange
    expected = "sac.no-raw-apptainer-build"
    # Act
    ids = [rule.id for rule in plugin.provide_hooks()]
    # Assert
    assert expected in ids


@_needs_contract
def test_raw_build_rule_denies_on_bash_pre_tool_use():
    # Arrange
    (rule,) = [
        r for r in plugin.provide_hooks() if r.id == "sac.no-raw-apptainer-build"
    ]
    # Act
    shape = (rule.event, rule.severity, rule.matches)
    # Assert
    assert shape == ("pre-tool-use", "deny", ("Bash",))


@_needs_contract
def test_every_rule_states_a_substantive_reason():
    # Arrange — the reason field is the auditable part; boilerplate defeats it
    rules = plugin.provide_hooks()
    # Act
    thin = [r.id for r in rules if len(r.reason) < 120]
    # Assert
    assert thin == []

# EOF
