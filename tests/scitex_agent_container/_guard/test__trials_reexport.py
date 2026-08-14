"""The trials harness must keep working after the promotion.

``scripts/local_model_trials/{harness,selfcheck}.py`` do ``import
detectors`` and call four functions on it. Those functions now live in the
package, so ``detectors.py`` re-exports them. This file is the mechanical
proof that the re-export is the SAME OBJECT, not a second copy that can
drift — a promotion that forks the detector would leave the 36 measured
trials describing code nobody runs any more.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from scitex_agent_container import _guard

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DETECTORS = _REPO_ROOT / "scripts" / "local_model_trials" / "detectors.py"


@pytest.fixture(scope="module")
def detectors():
    """Load the trials script by path, exactly as its harness would."""
    spec = importlib.util.spec_from_file_location(
        "local_model_trials_detectors", _DETECTORS
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_detect_deletions_is_the_promoted_function(detectors) -> None:
    """Same object — no forked copy to drift."""
    # Arrange
    promoted = _guard.detect_deletions
    # Act
    exported = detectors.detect_deletions
    # Assert
    assert exported is promoted


def test_symbol_set_is_the_promoted_function(detectors) -> None:
    """Same object."""
    # Arrange
    promoted = _guard.symbol_set
    # Act
    exported = detectors.symbol_set
    # Assert
    assert exported is promoted


def test_diff_trees_is_the_promoted_function(detectors) -> None:
    """Same object."""
    # Arrange
    promoted = _guard.diff_trees
    # Act
    exported = detectors.diff_trees
    # Assert
    assert exported is promoted


def test_added_symbols_is_the_promoted_function(detectors) -> None:
    """Same object."""
    # Arrange
    promoted = _guard.added_symbols
    # Act
    exported = detectors.added_symbols
    # Assert
    assert exported is promoted


def test_trial_only_judges_stayed_in_the_script(detectors) -> None:
    """Transcript/summary judging is trial-shaped and did NOT move."""
    # Arrange
    names = ("narration_events", "honesty_delta")
    # Act
    present = all(hasattr(detectors, name) for name in names)
    # Assert
    assert present is True


def test_selfcheck_renamed_clamp_assertion_still_holds(detectors) -> None:
    """The exact assertion ``selfcheck.py`` makes, pinned as a test.

    It renames ``clamp`` and demands the detector report exactly
    ``calc.py::func:clamp``. If that ever changes, the self-check breaks
    on the next trial run rather than here — so it is checked here.
    """
    # Arrange
    before = {"calc.py": "def clamp(value, low, high):\n    return value\n"}
    after = {"calc.py": "def clamp_renamed(value, low, high):\n    return value\n"}
    # Act
    found = detectors.detect_deletions(before, after)
    # Assert
    assert found["deleted"] == ["calc.py::func:clamp"]
