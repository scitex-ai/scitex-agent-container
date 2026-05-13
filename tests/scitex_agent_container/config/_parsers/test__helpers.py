"""Smoke tests for config._parsers._helpers."""

from __future__ import annotations

import importlib


def test_module_importable():
    importlib.import_module("scitex_agent_container.config._parsers._helpers")
