"""Shared fixtures for _listen tests.

Registered here so test modules request them by parameter name instead of
importing them (an imported fixture is redefined by every test parameter of
the same name, which ruff reports as F811).
"""

from .._django.test__control_listener_message import endpoints
from .test__agent_handshake import rig

__all__ = ["endpoints", "rig"]
