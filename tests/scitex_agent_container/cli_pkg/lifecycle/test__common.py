"""Smoke tests for cli_pkg.lifecycle._common."""

from __future__ import annotations

import importlib


def test_module_importable():
    importlib.import_module("scitex_agent_container.cli_pkg.lifecycle._common")
