"""Shared fixtures for runtimes tests.

Registered here so test modules request them by parameter name instead of
importing them (an imported fixture is redefined by every test parameter of
the same name, which ruff reports as F811). A module that defines its own
fixture of the same name still overrides these.
"""

from ._a2a_sidecar_fixture import shared_workdir_pair, workdir
from ._tui_turn_bridge_helpers import bridge_factory, isolated_home

__all__ = ["bridge_factory", "isolated_home", "shared_workdir_pair", "workdir"]
