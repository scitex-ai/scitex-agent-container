"""Shared fixtures for _runners tests.

Registered here so test modules request them by parameter name instead of
importing them (an imported fixture is redefined by every test parameter of
the same name, which ruff reports as F811).
"""

from ._a2a_proxy_main_fixture import main_short_run
from .test_a2a_proxy import real_upstream_server

__all__ = ["main_short_run", "real_upstream_server"]
