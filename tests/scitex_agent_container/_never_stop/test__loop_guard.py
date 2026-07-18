"""Tests for ``_never_stop._loop_guard`` — consecutive unproductive blocks.

PA-306 no-mocks: the counter is a real JSON file on a real filesystem,
redirected through the production ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
knob (read at call time by ``runtime_base_dir()``, so the redirect really
takes effect — no import-time constant to defeat it).
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._never_stop._loop_guard import (
    MAX_CONSECUTIVE_BLOCKS,
    clear_blocks,
    record_block,
    signature,
    state_path,
)

from ._fake_detector import isolate_runtime

_CARDS = ("card-a", "card-b")


def test_state_lands_under_the_redirected_runtime_dir(env_save_restore, tmp_path: Path):
    # Arrange
    root = isolate_runtime(env_save_restore, tmp_path)
    # Act
    path = state_path("agent-x")
    # Assert
    assert str(path).startswith(str(root))


def test_first_block_counts_one(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    # Act
    count, tripped = record_block("agent-x", _CARDS)
    # Assert
    assert (count, tripped) == (1, False)


def test_identical_set_increments(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _CARDS)
    # Act
    count, _ = record_block("agent-x", _CARDS)
    # Assert
    assert count == 2


def test_changed_card_set_resets_the_counter(env_save_restore, tmp_path: Path):
    """A changed runnable set IS observable progress — not a stuck loop."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _CARDS)
    record_block("agent-x", _CARDS)
    # Act
    count, tripped = record_block("agent-x", ("card-a", "card-c"))
    # Assert
    assert (count, tripped) == (1, False)


def test_reordered_card_set_is_not_progress(env_save_restore, tmp_path: Path):
    """Ordering noise from the detector must not read as a changed set."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", ("card-a", "card-b"))
    # Act
    count, _ = record_block("agent-x", ("card-b", "card-a"))
    # Assert
    assert count == 2


def test_guard_trips_after_max_consecutive_blocks(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS):
        record_block("agent-x", _CARDS)
    # Act
    count, tripped = record_block("agent-x", _CARDS)
    # Assert
    assert tripped is True and count == MAX_CONSECUTIVE_BLOCKS + 1


def test_guard_does_not_trip_at_exactly_max(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS - 1):
        record_block("agent-x", _CARDS)
    # Act
    _, tripped = record_block("agent-x", _CARDS)
    # Assert
    assert tripped is False


def test_tripping_resets_so_the_alarm_fires_once(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for _ in range(MAX_CONSECUTIVE_BLOCKS + 1):
        record_block("agent-x", _CARDS)
    # Act
    count, tripped = record_block("agent-x", _CARDS)
    # Assert
    assert (count, tripped) == (1, False)


def test_clear_blocks_forgets_history(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _CARDS)
    record_block("agent-x", _CARDS)
    # Act
    clear_blocks("agent-x")
    count, _ = record_block("agent-x", _CARDS)
    # Assert
    assert count == 1


def test_clear_blocks_on_absent_state_is_silent(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    # Act
    clear_blocks("never-seen-agent")
    # Assert
    assert not state_path("never-seen-agent").exists()


def test_agents_do_not_share_a_counter(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    record_block("agent-x", _CARDS)
    record_block("agent-x", _CARDS)
    # Act
    count, _ = record_block("agent-y", _CARDS)
    # Assert
    assert count == 1


def test_slashes_in_agent_name_cannot_escape_the_state_dir(
    env_save_restore, tmp_path: Path
):
    # Arrange
    root = isolate_runtime(env_save_restore, tmp_path)
    # Act
    path = state_path("../../etc/passwd")
    # Assert
    assert path.parent == root / "never_stop"


def test_empty_card_set_still_has_a_stable_signature(env_save_restore, tmp_path: Path):
    """An unparseable exit 2 repeating is still a repeat, so the empty set
    must hash consistently rather than looking like fresh progress."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    first = signature(())
    # Act
    second = signature([])
    # Assert
    assert first == second


def test_corrupt_state_file_is_treated_as_no_history(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    path = state_path("agent-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json")
    # Act
    count, tripped = record_block("agent-x", _CARDS)
    # Assert
    assert (count, tripped) == (1, False)
