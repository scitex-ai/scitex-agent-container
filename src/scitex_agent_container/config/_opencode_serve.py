"""Opencode serve port from the selected harness entry.

``serve.port: auto`` (the default) leaves port allocation to the start
path, which persists the chosen loopback URL into the incarnation
state. An explicit positive integer pins the loopback serve port for
single-agent hosts. ``None`` means auto.
"""

from __future__ import annotations

from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness

DEFAULT_OPENCODE_SERVE_PORT: int | None = None


def parse_selected_opencode_serve_port(spec: Mapping) -> int | None:
    """Return the selected opencode entry's serve port, if pinned."""
    if canonical_harness(resolve_spec_harness(spec)) != "opencode":
        return DEFAULT_OPENCODE_SERVE_PORT
    harnesses = spec.get("available_harnesses")
    if not isinstance(harnesses, Mapping):
        return DEFAULT_OPENCODE_SERVE_PORT
    for key, value in harnesses.items():
        if canonical_harness(str(key)) == "opencode" and isinstance(value, Mapping):
            serve = value.get("serve", {})
            if not isinstance(serve, Mapping):
                raise ValueError("serve must be a mapping")
            port = serve.get("port", "auto")
            if port == "auto":
                return DEFAULT_OPENCODE_SERVE_PORT
            if type(port) is not int or port <= 0:
                raise ValueError("serve.port must be 'auto' or a positive integer")
            return port
    return DEFAULT_OPENCODE_SERVE_PORT


__all__ = ["DEFAULT_OPENCODE_SERVE_PORT", "parse_selected_opencode_serve_port"]
