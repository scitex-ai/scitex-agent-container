"""The promoted symbol primitives must keep the semantics they were
measured with — and the trials harness must still get the SAME objects.

These functions judged 36 local-model coding trials before they moved into
the package. If the promotion quietly changed what counts as a symbol, the
36 results stop meaning what we said they mean — so the contract is pinned
here rather than left to the reader of a diff.

The re-export half lives here too, rather than in its own file: the four
primitives ``scripts/local_model_trials/detectors.py`` re-exports are all
defined in ``_symbols.py``, so this is their mirror (PS-204 §2 — a test
file must mirror a source file, and there is no ``_trials_reexport.py``
for a ``test__trials_reexport.py`` to mirror).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from scitex_agent_container import _guard
from scitex_agent_container._guard import (
    added_symbols,
    detect_deletions,
    diff_trees,
    symbol_locations,
    symbol_set,
)

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

SOURCE = '''\
def top_level():
    def nested():
        return 1
    return nested


class Widget:
    def method(self):
        return 2


async def coro():
    return 3
'''


def test_module_level_function_is_a_symbol() -> None:
    """'func:name' for a module-level def."""
    # Arrange
    source = SOURCE
    # Act
    syms = symbol_set(source)
    # Assert
    assert "func:top_level" in syms


def test_async_function_is_a_symbol() -> None:
    """async def counts exactly like def."""
    # Arrange
    source = SOURCE
    # Act
    syms = symbol_set(source)
    # Assert
    assert "func:coro" in syms


def test_class_and_its_method_are_symbols() -> None:
    """A method is 'class:Name.method', so losing one is visible."""
    # Arrange
    source = SOURCE
    # Act
    syms = symbol_set(source)
    # Assert
    assert {"class:Widget", "class:Widget.method"} <= syms


def test_nested_function_is_not_a_symbol() -> None:
    """A nested helper is not importable, so its loss is a different event."""
    # Arrange
    source = SOURCE
    # Act
    syms = symbol_set(source)
    # Assert
    assert "func:nested" not in syms


def test_syntax_error_yields_none_not_empty() -> None:
    """None means 'cannot tell'; an empty set would mean 'nothing here'."""
    # Arrange
    source = "class Broken(:\n"
    # Act
    syms = symbol_set(source)
    # Assert
    assert syms is None


def test_symbol_locations_reports_the_line_span() -> None:
    """The span is what turns a missing name into an actionable error."""
    # Arrange
    source = SOURCE
    # Act
    located = symbol_locations(source)
    # Assert
    assert located["class:Widget"] == (7, 9)


def test_symbol_locations_is_none_on_syntax_error() -> None:
    """Same three-valued honesty as symbol_set."""
    # Arrange
    source = "def (:\n"
    # Act
    located = symbol_locations(source)
    # Assert
    assert located is None


def test_deleted_symbol_is_reported() -> None:
    """The core judgement: a symbol present before and absent after."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"}
    after = {"m.py": "def a():\n    pass\n"}
    # Act
    found = detect_deletions(before, after)
    # Assert
    assert found["deleted"] == ["m.py::func:b"]


def test_allowed_deletion_moves_to_allowed_hits() -> None:
    """A requested deletion is recorded, not counted against the change."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"}
    after = {"m.py": "def a():\n    pass\n"}
    # Act
    found = detect_deletions(before, after, allowed={"m.py::func:b"})
    # Assert
    assert found["deleted"] == []


def test_vanished_file_is_reported() -> None:
    """A whole file disappearing is a deletion too."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n", "notes.txt": ""}
    after = {"m.py": "def a():\n    pass\n"}
    # Act
    found = detect_deletions(before, after)
    # Assert
    assert found["deleted_files"] == ["notes.txt"]


def test_unparsable_file_is_reported_separately() -> None:
    """A file that no longer parses is skipped — and named as skipped."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"}
    after = {"m.py": "def a(:\n"}
    # Act
    found = detect_deletions(before, after)
    # Assert
    assert found["broken_files"] == ["m.py"]


def test_unparsable_file_hides_its_deletions() -> None:
    """Documents the UNKNOWN the report layer must not read as clean."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"}
    after = {"m.py": "def a(:\n"}
    # Act
    found = detect_deletions(before, after)
    # Assert
    assert found["deleted"] == []


def test_diff_trees_separates_changed_added_removed() -> None:
    """File-level view used by the trials harness."""
    # Arrange
    before = {"a.py": "1", "b.py": "2"}
    after = {"a.py": "9", "c.py": "3"}
    # Act
    diff = diff_trees(before, after)
    # Assert
    assert diff == {"changed": ["a.py"], "added": ["c.py"],
                    "removed": ["b.py"]}


def test_added_symbols_lists_new_definitions() -> None:
    """The mirror of the deletion diff."""
    # Arrange
    before = {"m.py": "def a():\n    pass\n"}
    after = {"m.py": "def a():\n    pass\n\n\ndef b():\n    pass\n"}
    # Act
    added = added_symbols(before, after)
    # Assert
    assert added == ["m.py::func:b"]


# --- the trials harness still gets these exact objects ---------------------
#
# harness.py / selfcheck.py do ``import detectors`` and call four functions
# on it. A promotion that FORKED the detector would leave the 36 measured
# trials describing code nobody runs any more, so identity is asserted, not
# behaviour-equivalence.


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
    after = {
        "calc.py": "def clamp_renamed(value, low, high):\n    return value\n"
    }
    # Act
    found = detectors.detect_deletions(before, after)
    # Assert
    assert found["deleted"] == ["calc.py::func:clamp"]
