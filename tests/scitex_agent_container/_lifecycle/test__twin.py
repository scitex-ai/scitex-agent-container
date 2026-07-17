"""``_lifecycle._twin`` — the twin facade.

The behaviour suites live beside the modules they cover
(``test__twin_identity`` / ``test__twin_derive`` / ``test__twin_seed``). This
file guards the ONE thing the facade itself promises: that
``from ._twin import ...`` keeps resolving the whole public API.

That promise is load-bearing rather than cosmetic — ``_start.agent_start``,
``cli_pkg/lifecycle/_twin`` and the ``agent_twin`` MCP tool all import through
this module, and ADR-0019 pins it as the single import surface for twin logic.
A split that quietly dropped a re-export would break those callers at runtime,
not at test time, so the import IS the test.
"""

from __future__ import annotations

import scitex_agent_container._lifecycle._twin as twin_facade


def test_facade_exports_the_documented_public_api():
    # Arrange — every name ADR-0019's callers import through the facade.
    expected = {
        "SELF_NAME_ENV",
        "TODO_AGENT_ENV",
        "TWIN_NAME_INFIX",
        "TWIN_PARENT_ENV",
        "TWIN_SESSION_NAMESPACE",
        "TwinIdentityError",
        "TwinSeedError",
        "assert_twin_identity",
        "build_twin_boot_kick",
        "derive_twin_spec",
        "prepare_twin_spawn",
        "resolve_twin_name",
        "seed_twin_from_parent",
        "twin_name_for_tag",
        "twin_session_uuid",
        "validate_twin_tag",
    }
    # Act
    missing = {name for name in expected if not hasattr(twin_facade, name)}
    # Assert
    assert missing == set()


def test_facade_all_matches_what_it_actually_exports():
    # Arrange — a name in __all__ that isn't bound is an import-time trap
    # for `from ._twin import *` and a lie to readers.
    declared = set(twin_facade.__all__)
    # Act
    unbound = {name for name in declared if not hasattr(twin_facade, name)}
    # Assert
    assert unbound == set()


def test_facade_errors_share_one_hierarchy():
    # Arrange — callers catch TwinSeedError to cover every twin failure,
    # including identity refusals.
    identity_error = twin_facade.TwinIdentityError
    # Act
    is_subclass = issubclass(identity_error, twin_facade.TwinSeedError)
    # Assert
    assert is_subclass is True
