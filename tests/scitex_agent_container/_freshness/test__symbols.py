"""Symbol probes against REAL modules (PA-306: no mocks).

The UNKNOWN case is driven by writing a real module to ``tmp_path`` that
imports a genuinely-missing dependency, then importing it for real. That
is the actual failure we must distinguish from "the symbol is absent",
and the only way to test it honestly is to cause it.
"""

from __future__ import annotations

import sys

from scitex_agent_container._freshness._symbols import (
    EXPECTATIONS,
    SymbolExpectation,
    probe,
    probe_all,
)

OFF_LOOP = "scitex_agent_container._lifecycle._off_loop"


def test_registered_fix_is_present_in_loaded_code():
    """#658's counter really is in this checkout — probed, not assumed.

    This is the canonical example: `abandoned_call_count` exists ONLY in
    the fixed `_off_loop`, so `hasattr` answers "is the fix in the code I
    am running" without consulting any version string.
    """
    # Arrange
    expectation = SymbolExpectation(
        module=OFF_LOOP,
        symbol="abandoned_call_count",
        since="0.21.15",
        why="#658 shared-executor thread leak",
    )

    # Act
    result = probe(expectation)

    # Assert
    assert result is True


def test_absent_symbol_probes_false():
    """A real module, a symbol that is not in it => the fix is not here."""
    # Arrange
    expectation = SymbolExpectation(
        module=OFF_LOOP,
        symbol="a_symbol_that_was_never_written",
        since="9.9.9",
        why="a fix that does not exist",
    )

    # Act
    result = probe(expectation)

    # Assert
    assert result is False


def test_absent_module_probes_false():
    """The module itself missing is still positive evidence the fix is not here."""
    # Arrange
    expectation = SymbolExpectation(
        module="scitex_agent_container._a_module_that_never_shipped",
        symbol="whatever",
        since="9.9.9",
        why="a module that does not exist",
    )

    # Act
    result = probe(expectation)

    # Assert
    assert result is False


def test_broken_dependency_probes_unknown(tmp_path):
    """A module whose OWN dependency is broken tells us nothing about our symbol.

    Returning False here would be a false RED — "the fix is missing!" when
    the truth is "this module's import chain is broken". A false RED is the
    dangerous kind, because someone acts on it. So: UNKNOWN.
    """
    # Arrange — a real module that really fails to import, for a reason
    # that has nothing to do with the symbol we are asking about.
    broken = tmp_path / "sac_freshness_broken_probe.py"
    broken.write_text("import a_package_that_is_not_installed_anywhere\n")
    sys.path.insert(0, str(tmp_path))
    expectation = SymbolExpectation(
        module="sac_freshness_broken_probe",
        symbol="anything",
        since="9.9.9",
        why="its dependency is missing, which is not evidence about us",
    )

    # Act
    try:
        result = probe(expectation)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("sac_freshness_broken_probe", None)

    # Assert
    assert result is None


def test_shipped_registry_probes_clean_here():
    """Every registered expectation holds in this checkout.

    If this fails, either a registered symbol was renamed/removed (fix the
    registry) or the registry is claiming a fix that never landed (fix the
    claim). Both are bugs worth failing the build over — a registry that
    lies is worse than an empty one.
    """
    # Arrange
    # Act
    results = probe_all(EXPECTATIONS)

    # Assert
    assert all(present is True for _, present in results)


# EOF
