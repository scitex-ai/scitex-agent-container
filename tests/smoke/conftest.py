"""Smoke-layer pytest config.

The `smoke` marker is *also* registered in pyproject.toml so
`pytest -m smoke` works from any directory; this conftest keeps the
layer self-contained for partial checkouts.
"""

from __future__ import annotations


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: fast user-facing happy-path tests (<60s total); "
        "runs on every PR. Opt out with '-m \"not smoke\"'.",
    )
