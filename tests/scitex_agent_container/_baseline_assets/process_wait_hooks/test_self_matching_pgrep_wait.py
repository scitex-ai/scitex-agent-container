"""Mirror tests for ``self_matching_pgrep_wait.py`` pure decisions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CORE_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "process_wait_hooks"
    / "self_matching_pgrep_wait.py"
)
_spec = importlib.util.spec_from_file_location(
    "self_matching_pgrep_wait", _CORE_PATH
)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


def test_core_module_file_exists() -> None:
    # Arrange
    path = _CORE_PATH
    # Act
    present = path.is_file()
    # Assert
    assert present, f"missing core module: {path}"


def test_literal_full_pattern_is_self_matching() -> None:
    # Arrange
    command = "until ! pgrep -f worker-name; do sleep 1; done"
    # Act
    pattern = core.self_matching_pattern(command)
    # Assert
    assert pattern == "worker-name"


def test_bracket_pattern_is_not_self_matching() -> None:
    # Arrange
    command = "while pgrep -f '[w]orker-name'; do sleep 1; done"
    # Act
    pattern = core.self_matching_pattern(command)
    # Assert
    assert pattern is None

# EOF
