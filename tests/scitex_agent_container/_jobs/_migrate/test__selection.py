"""Tests for the run-selection knob.

THE THIRD STATE IS THE POINT. ``selection()`` returns ``None`` for
UNSTATED, which must never collapse into "nothing selected". A host that
has never been configured must not have its timers disarmed by the mere
arrival of this feature; a host that deliberately selected nothing must
not have them armed. Those two are one line apart in the code and a fleet
outage apart in effect, so each has its own test.

No mocks (PA-306): ``selection`` takes its env as a plain dict and its
home as a ``tmp_path``.
"""

from __future__ import annotations

from scitex_agent_container._jobs._migrate import _selection


def _write(home, body: str):
    path = _selection.selection_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_blank_lines_are_ignored() -> None:
    # Arrange
    body = "worktree-gc\n\n\nfleet-reconcile\n"
    # Act
    got = _selection.parse_selection(body)
    # Assert
    assert got == frozenset({"worktree-gc", "fleet-reconcile"})


def test_a_full_line_comment_is_ignored() -> None:
    # Arrange
    body = "# not a job\nworktree-gc\n"
    # Act
    got = _selection.parse_selection(body)
    # Assert
    assert got == frozenset({"worktree-gc"})


def test_a_trailing_comment_is_stripped() -> None:
    # Arrange
    body = "worktree-gc  # daily GC\n"
    # Act
    got = _selection.parse_selection(body)
    # Assert
    assert got == frozenset({"worktree-gc"})


def test_commas_separate_names_exactly_like_newlines() -> None:
    # Arrange — one grammar for the env form and the file form, so an
    # operator who learns one has learned the other.
    body = "worktree-gc,fleet-reconcile"
    # Act
    got = _selection.parse_selection(body)
    # Assert
    assert got == frozenset({"worktree-gc", "fleet-reconcile"})


def test_the_env_override_wins_over_the_file(tmp_path) -> None:
    # Arrange
    _write(tmp_path, "fleet-reconcile\n")
    env = {_selection.SELECTION_ENV: "worktree-gc"}
    # Act
    got = _selection.selection(env=env, home=tmp_path)
    # Assert
    assert got == frozenset({"worktree-gc"})


def test_the_file_is_read_when_the_env_is_absent(tmp_path) -> None:
    # Arrange
    _write(tmp_path, "fleet-reconcile\n")
    # Act
    got = _selection.selection(env={}, home=tmp_path)
    # Assert
    assert got == frozenset({"fleet-reconcile"})


def test_a_blank_env_falls_through_to_the_file(tmp_path) -> None:
    # Arrange — an exported-but-empty variable is not a statement.
    _write(tmp_path, "fleet-reconcile\n")
    env = {_selection.SELECTION_ENV: "   "}
    # Act
    got = _selection.selection(env=env, home=tmp_path)
    # Assert
    assert got == frozenset({"fleet-reconcile"})


def test_an_absent_file_is_unstated_not_empty(tmp_path) -> None:
    # Arrange — THE distinction. Returning an empty set here would disarm
    # every host that never opted in.
    # Act
    got = _selection.selection(env={}, home=tmp_path)
    # Assert
    assert got is None


def test_an_empty_file_is_a_deliberate_empty_selection(tmp_path) -> None:
    # Arrange — the operator wrote the file and listed nothing. That is a
    # statement, and it must not read as "unstated".
    _write(tmp_path, "# nothing runs here\n")
    # Act
    got = _selection.selection(env={}, home=tmp_path)
    # Assert
    assert got == frozenset()


def test_an_unstated_selection_treats_every_job_as_eligible() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, None)
    # Assert
    assert got is True


def test_a_deliberately_empty_selection_selects_nothing() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, frozenset())
    # Assert
    assert got is False


def test_the_star_token_selects_every_job() -> None:
    # Arrange — "run everything" must be writable down, not only implied.
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, frozenset({_selection.SELECT_ALL}))
    # Assert
    assert got is True


def test_a_local_name_in_the_selection_matches_the_canonical_job() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, frozenset({"worktree-gc"}))
    # Assert
    assert got is True


def test_a_canonical_name_in_the_selection_matches() -> None:
    # Arrange — a name pasted out of --json output must work as typed.
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, frozenset({name}))
    # Assert
    assert got is True


def test_a_job_absent_from_a_stated_selection_is_not_selected() -> None:
    # Arrange
    name = "scitex-agent-container-worktree-gc"
    # Act
    got = _selection.is_selected(name, frozenset({"fleet-reconcile"}))
    # Assert
    assert got is False


def test_explain_says_unstated_when_nothing_is_configured() -> None:
    # Arrange — a knob whose setting cannot be read back is a knob nobody
    # trusts, so every command that consults it says what it found.
    chosen = None
    # Act
    got = _selection.explain(chosen)
    # Assert
    assert "UNSTATED" in got


def test_explain_says_empty_when_the_selection_is_deliberately_empty() -> None:
    # Arrange
    chosen = frozenset()
    # Act
    got = _selection.explain(chosen)
    # Assert
    assert "EMPTY" in got


def test_explain_names_the_star_token_when_everything_is_selected() -> None:
    # Arrange
    chosen = frozenset({_selection.SELECT_ALL})
    # Act
    got = _selection.explain(chosen)
    # Assert
    assert _selection.SELECT_ALL in got


def test_explain_lists_the_selected_jobs() -> None:
    # Arrange
    chosen = frozenset({"worktree-gc", "fleet-reconcile"})
    # Act
    got = _selection.explain(chosen)
    # Assert
    assert "fleet-reconcile, worktree-gc" in got
