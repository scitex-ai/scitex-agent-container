"""Smoke tests for _state.snapshot._io."""

from __future__ import annotations

import importlib


def test_module_importable():
    importlib.import_module("scitex_agent_container._state.snapshot._io")
