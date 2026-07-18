"""The raw-observation archive: whole captures, MARKED truncation, real files.

「状態とった時に全ログを取っておいてくださいね？」 — and the reason is specific.
Every diagnosis in the 2026-07-17/18 incident that reached a true answer did so
from RAW TEXT; every one that went wrong had only verdicts. A verdict cannot be
re-examined after the fact, so this suite pins the two properties that make the
archive worth having: the bytes survive, and when they cannot, the cut SAYS SO.

Real temp files throughout — no mocks, no patched filesystem.
"""

from __future__ import annotations

from scitex_agent_container._agentstate import (
    AgentState,
    append_state,
    mark_truncated,
    read_journal,
)
from scitex_agent_container._agentstate._journal import TRUNCATION_MARKER

#: A pane capture with an embedded timestamp — the shape that actually answered
#: a question during the incident, as opposed to the summaries that did not.
PANE = "● Login expired · Please run /login\n[20:20:03] idle\n────────\n❯\n"


def journal(tmp_path):
    return tmp_path / "agent-state.jsonl"


def test_a_reading_is_appended_as_one_json_line(tmp_path):
    """One line per record, so grep works."""
    # Arrange
    state = AgentState(agent="alpha", is_tmux_live=True)
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    assert len(journal(tmp_path).read_text().splitlines()) == 1


def test_the_full_pane_capture_survives_the_round_trip(tmp_path):
    """Not a tail — the WHOLE pane. A tail slice is how an investigator watched a
    countdown widget change and nearly concluded 'it is working'."""
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": PANE})
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    assert next(read_journal(journal(tmp_path)))["raw"]["pane_run1"] == PANE


def test_both_capture_runs_are_kept(tmp_path):
    """The frozen check needs two reads, so both must be re-examinable."""
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": PANE, "pane_run2": PANE})
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    assert next(read_journal(journal(tmp_path)))["raw"]["pane_run2"] == PANE


def test_the_ps_line_with_its_start_time_is_kept(tmp_path):
    """'this pid exists' and 'this pid is OURS' differ, and lstart is the difference."""
    # Arrange
    ps = "2620416 Fri Jul 18 09:12:31 2026 Sl+ apptainer exec claude"
    state = AgentState(agent="alpha", raw={"ps_line": ps})
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    assert next(read_journal(journal(tmp_path)))["raw"]["ps_line"] == ps


def test_the_reason_for_a_none_is_archived(tmp_path):
    """Every None must carry WHY into the archive, not only into the console."""
    # Arrange
    state = AgentState(
        agent="alpha", reasons={"is_tmux_live": "socket in another namespace"}
    )
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    record = next(read_journal(journal(tmp_path)))
    assert record["signals"]["is_tmux_live"]["reason"] == "socket in another namespace"


def test_the_verdict_is_archived_beside_the_raw_evidence(tmp_path):
    """Both, always: the evidence is what makes the verdict checkable later."""
    # Arrange
    state = AgentState.unknown("scitex-hub", "never read")
    # Act
    append_state(state, path=journal(tmp_path))
    # Assert
    assert next(read_journal(journal(tmp_path)))["assessment"]["exit_code"] == 2


# ---------------------------------------------------------------------------
# Truncation is BOUNDED but never SILENT.
# ---------------------------------------------------------------------------


def test_a_capture_under_the_cap_is_untouched():
    # Arrange
    text = "short"
    # Act
    kept, was_cut, _ = mark_truncated(text, 1024)
    # Assert
    assert kept == text


def test_a_capture_under_the_cap_is_not_flagged_as_cut():
    """The CONTROL: if everything reported truncated, the flag would mean nothing."""
    # Arrange
    text = "short"
    # Act
    _, was_cut, _ = mark_truncated(text, 1024)
    # Assert
    assert was_cut is False


def test_an_oversized_capture_is_marked_in_the_text_itself():
    """A capture that LOOKS complete but is not is worse than one that admits it."""
    # Arrange
    text = "x" * 5000
    # Act
    kept, _, _ = mark_truncated(text, 100)
    # Assert
    assert "SAC-AGENTSTATE-TRUNCATED" in kept


def test_the_truncation_marker_states_the_original_size():
    """'Something is missing' is not enough — a reader needs to know HOW MUCH."""
    # Arrange
    text = "x" * 5000
    # Act
    kept, _, _ = mark_truncated(text, 100)
    # Assert
    assert "of 5000B" in kept


def test_an_oversized_capture_reports_its_true_original_length():
    # Arrange
    text = "x" * 5000
    # Act
    _, _, total = mark_truncated(text, 100)
    # Assert
    assert total == 5000


def test_truncation_is_measured_in_bytes_not_characters():
    """A multi-byte capture would otherwise blow past a character budget."""
    # Arrange
    text = "あ" * 100  # 300 bytes in UTF-8
    # Act
    _, _, total = mark_truncated(text, 1000)
    # Assert
    assert total == 300


def test_a_truncated_capture_is_named_in_the_write_result(tmp_path):
    """The caller learns a cut happened without having to scan the text."""
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": "x" * 5000})
    # Act
    write = append_state(state, path=journal(tmp_path), max_capture_bytes=100)
    # Assert
    assert write.truncated == ("pane_run1",)


def test_a_truncated_capture_is_flagged_as_data_in_the_record(tmp_path):
    """A JSON consumer must see the cut without string-matching for a marker."""
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": "x" * 5000})
    # Act
    append_state(state, path=journal(tmp_path), max_capture_bytes=100)
    # Assert
    assert "pane_run1" in next(read_journal(journal(tmp_path)))["truncated"]


def test_an_untruncated_write_reports_nothing_cut(tmp_path):
    """The CONTROL for the two gates above."""
    # Arrange
    state = AgentState(agent="alpha", raw={"pane_run1": PANE})
    # Act
    write = append_state(state, path=journal(tmp_path))
    # Assert
    assert write.truncated == ()


# ---------------------------------------------------------------------------
# Rotation MOVES bytes; it never condenses them.
# ---------------------------------------------------------------------------


def test_an_oversized_journal_rotates(tmp_path):
    # Arrange
    path = journal(tmp_path)
    path.write_text("old evidence\n")
    # Act
    write = append_state(AgentState(agent="alpha"), path=path, max_bytes=1)
    # Assert
    assert write.rotated is True


def test_rotation_preserves_the_previous_generation_verbatim(tmp_path):
    """Solving size by SUMMARISING would reintroduce the exact problem."""
    # Arrange
    path = journal(tmp_path)
    path.write_text("old evidence\n")
    # Act
    append_state(AgentState(agent="alpha"), path=path, max_bytes=1)
    # Assert
    assert path.with_suffix(".jsonl.1").read_text() == "old evidence\n"


def test_a_small_journal_does_not_rotate(tmp_path):
    """The CONTROL: rotation must be driven by size, not by every write."""
    # Arrange
    path = journal(tmp_path)
    # Act
    write = append_state(AgentState(agent="alpha"), path=path)
    # Assert
    assert write.rotated is False


# ---------------------------------------------------------------------------
# Failures are reported, never swallowed.
# ---------------------------------------------------------------------------


def test_an_unwritable_journal_reports_failure_instead_of_raising(tmp_path):
    """Losing the OBSERVATION would be worse than losing the archive of it."""
    # Arrange
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    # Act
    write = append_state(AgentState(agent="alpha"), path=blocked / "x.jsonl")
    # Assert
    assert write.ok is False


def test_a_failed_write_says_what_was_lost(tmp_path):
    """A log that quietly stopped writing looks exactly like a quiet fleet."""
    # Arrange
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    # Act
    write = append_state(AgentState(agent="alpha"), path=blocked / "x.jsonl")
    # Assert
    assert "will not be re-examinable" in write.detail


def test_a_successful_write_reports_success(tmp_path):
    # Arrange
    path = journal(tmp_path)
    # Act
    write = append_state(AgentState(agent="alpha"), path=path)
    # Assert
    assert write.ok is True


def test_reading_an_absent_journal_yields_nothing_rather_than_raising(tmp_path):
    """Nothing recorded yet is a legitimate empty read."""
    # Arrange
    path = tmp_path / "never-written.jsonl"
    # Act
    records = list(read_journal(path))
    # Assert
    assert records == []


def test_a_torn_line_is_surfaced_as_data_not_dropped(tmp_path):
    """An iterator that skips damaged records reports a cleaner history than exists."""
    # Arrange
    path = journal(tmp_path)
    path.write_text('{"agent": "alpha"}\n{not json\n')
    # Act
    records = list(read_journal(path))
    # Assert
    assert records[1]["_unparseable"] == "{not json"


def test_the_marker_constant_is_greppable():
    """One search must find every truncated record in the archive."""
    # Arrange
    marker = TRUNCATION_MARKER
    # Act
    rendered = marker.format(kept=1, total=2)
    # Assert
    assert "SAC-AGENTSTATE-TRUNCATED" in rendered


# EOF
