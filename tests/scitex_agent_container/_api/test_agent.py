"""Tests for ``scitex_agent_container._api`` noun submodules.

After the cleanup that dropped flat ``sac.agent_list`` re-exports
in favour of nested ``sac.agent.list``, these tests guarantee:

  1. Every CLI-tree verb is reachable via the noun submodule.
  2. The package root only carries noun submodules + a handful of
     legacy non-grouped names (config, Registry, …).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp")

import scitex_agent_container as sac  # noqa: E402


@pytest.mark.parametrize(
    "submodule, verb",
    [
        ("agent", "list"),
        ("agent", "status"),
        ("agent", "logs"),
        ("agent", "start"),
        ("agent", "stop"),
        ("agent", "restart"),
        ("agent", "attach"),
        ("agent", "check"),
        ("agent", "validate"),
        ("agent", "inspect"),
        ("agent", "find"),
        ("agent", "recall"),
        ("agent", "check_priority"),
        ("agent", "take_snapshot"),
        ("agent", "health"),
        ("db", "show"),
        ("db", "query"),
        ("db", "clean"),
        ("db", "tick"),
        ("db", "migrate"),
        ("db", "export"),
        ("db", "import_"),
        ("host", "show"),
        ("host", "list"),
        ("host", "validate"),
        ("host", "probe"),
        ("host", "exec"),
        ("image", "build"),
        ("template", "render_contributor_spec"),
        ("account", "show"),
        ("account", "watch_quota"),
        ("skills", "list"),
        ("skills", "get"),
        ("mcp", "list_tools"),
        ("mcp", "doctor"),
    ],
)
def test_nested_verb_is_callable(submodule: str, verb: str) -> None:
    """Every CLI-tree verb is reachable + callable via the noun submodule."""
    # Arrange
    noun_module = getattr(sac, submodule)
    # Act
    fn = getattr(noun_module, verb)
    # Assert
    assert callable(fn), f"sac.{submodule}.{verb} should be callable"


@pytest.mark.parametrize(
    "noun",
    ["agent", "db", "host", "image", "template", "account", "skills", "mcp"],
)
def test_every_submodule_listed_in_package_all(noun: str) -> None:
    """The eight noun submodules must appear in ``sac.__all__`` so
    Sphinx + the linter discover them."""
    # Arrange
    all_names = sac.__all__
    # Act
    is_listed = noun in all_names
    # Assert
    assert is_listed, f"{noun!r} missing from sac.__all__"


def test_no_flat_verb_duplicates_at_package_root() -> None:
    """We removed flat names like ``sac.agent_list`` in favour of the
    nested form. Guard against accidental re-exports creeping back in."""
    # Arrange
    permitted = {
        "AgentConfig",
        "Registry",
        "load_config",
        "validate_config",
        "peer",
        "agent",
        "db",
        "host",
        "image",
        "template",
        "account",
        "skills",
        "mcp",
        "__version__",
    }
    # Act
    leaked = [
        name
        for name in sac.__all__
        if name not in permitted and "_" in name and not name.startswith("_")
    ]
    # Assert
    assert leaked == [], (
        f"flat verb_noun duplicates leaked into sac.__all__: {leaked}. "
        f"Use the nested form (sac.<noun>.<verb>) instead."
    )
