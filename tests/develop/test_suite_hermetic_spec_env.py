"""The suite must not inherit the container's spec-env manifest.

Guards the ``os.environ.pop("SAC_SPEC_ENV_KEYS", None)`` floor in
``tests/conftest.py``. Without it, running pytest inside a sac agent
container makes ``resolve_spec_env`` fire during ``build_sdk_options`` and
kills 25 tests that have nothing to do with spec-env.

WHY THIS TEST EXISTS RATHER THAN JUST THE FLOOR: the failure is invisible
from CI. A GitHub runner is not an agent container, so the var is never set
there and the gate is green whether or not the floor survives. If someone
removes it, CI keeps passing and every agent in the fleet silently starts
seeing the same 25 false failures again — which is how this went unnoticed
for a day, and was then carded WRONGLY as "develop is RED before any
change" (2026-08-17). So the regression has to be caught by an assertion
that runs everywhere, not by a red gate only a container can produce.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.runtimes._mcp_spec_env import (
    SPEC_ENV_KEYS_VAR,
    SpecEnvUnresolvedError,
    resolve_spec_env,
)


def test_conftest_env_floor_is_in_effect() -> None:
    """POSITIVE CONTROL for the absence assertion in the next test.

    An is-absent assertion also passes against an empty or unexpected
    ``os.environ``. This pins that we are looking at the mapping conftest
    configured, so the absence below is a real absence.
    """
    # Arrange: conftest's module body force-sets this floor at import time.
    expected_floor = "1"

    # Act
    observed = os.environ.get("SAC_BUILD_NO_NICE")

    # Assert
    assert observed == expected_floor


def test_spec_env_manifest_is_not_inherited_from_the_ambient_environment() -> None:
    """The launch-injected manifest must be absent for the whole session."""
    # Arrange: nothing to build — the floor runs in conftest's module body,
    # before any test module is imported.
    leak_hint = (
        f"{SPEC_ENV_KEYS_VAR} leaked into the test session from the "
        f"surrounding agent container. resolve_spec_env() reads the real "
        f"os.environ and raises SpecEnvUnresolvedError for every key named "
        f"there that the test env does not define, which kills unrelated "
        f"tests (25 of them, measured on develop @4a03f69c). Restore the "
        f"os.environ.pop({SPEC_ENV_KEYS_VAR!r}, None) floor in "
        f"tests/conftest.py."
    )

    # Act
    present = SPEC_ENV_KEYS_VAR in os.environ

    # Assert
    assert present is False, leak_hint


def test_guard_still_refuses_when_a_promised_key_is_absent() -> None:
    """Clearing the ambient var must not disarm the production guard.

    The floor deletes a leaked value; it must not make the refusal
    untestable. Without this, the fix could turn a working gate into a gate
    that cannot fail.
    """
    # Arrange: an EXPLICIT mapping, never the ambient one, promising a key
    # it does not carry.
    environ = {SPEC_ENV_KEYS_VAR: "A_KEY_THAT_IS_NOT_PRESENT"}

    # Act / Assert are one statement for a raising call: the context manager
    # performs the call and checks both the type and that the message names
    # the offending key.
    # Act
    def _call() -> dict[str, str]:
        return resolve_spec_env(environ)

    # Assert
    with pytest.raises(SpecEnvUnresolvedError, match="A_KEY_THAT_IS_NOT_PRESENT"):
        _call()
