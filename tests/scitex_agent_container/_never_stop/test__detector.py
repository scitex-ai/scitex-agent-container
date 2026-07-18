"""Tests for ``_never_stop._detector`` — the three-state may-stop probe.

PA-306 no-mocks: every probe spawns a REAL executable written to
``tmp_path`` that prints real streams and exits with a real code, reached
through the production ``$SAC_MAY_STOP_CMD`` knob. Nothing is patched.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._never_stop._detector import (
    ALLOW,
    RUNNABLE,
    UNKNOWN,
    detector_argv,
    probe,
)

from ._fake_detector import (
    detector_env,
    hint_block,
    missing_detector,
    runnable_payload,
    write_detector,
)

_ITEMS = [
    ("sac-card-1", "in_progress, untouched 3h", "Run the failing test and fix it"),
    ("sac-card-2", "unread inbox", "Poll your inbox and act on the digest"),
]


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_default_argv_calls_scitex_cards_may_stop(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_MAY_STOP_CMD")
    # Act
    argv = detector_argv("scitex-hub")
    # Assert
    assert argv == ["scitex-cards", "may-stop", "--agent", "scitex-hub"]


def test_argv_always_names_the_agent(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_MAY_STOP_CMD", "/usr/bin/custom detector --flag")
    # Act
    argv = detector_argv("worker-7")
    # Assert
    assert argv[-2:] == ["--agent", "worker-7"]


# ---------------------------------------------------------------------------
# exit 0 — definite ALLOW
# ---------------------------------------------------------------------------


def test_exit_zero_is_allow(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(
        tmp_path,
        returncode=0,
        stdout='{"agent":"a","runnable":false,"items":[],"idle_seconds":null}',
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == ALLOW


def test_exit_zero_carries_no_items(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(tmp_path, returncode=0)
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.items == ()


# ---------------------------------------------------------------------------
# exit 2 — definite RUNNABLE
# ---------------------------------------------------------------------------


def test_exit_two_is_runnable(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(
        tmp_path,
        returncode=2,
        stdout=runnable_payload(_ITEMS),
        stderr=hint_block(_ITEMS),
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == RUNNABLE


def test_exit_two_parses_next_actions_from_stdout_json(
    env_save_restore, tmp_path: Path
):
    # Arrange
    script = write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS))
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert [i.next_action for i in verdict.items] == [a for _, _, a in _ITEMS]


def test_exit_two_parses_card_ids(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS))
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.card_ids == ("sac-card-1", "sac-card-2")


def test_exit_two_reads_idle_seconds(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(
        tmp_path, returncode=2, stdout=runnable_payload(_ITEMS, idle=4820)
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.idle_seconds == 4820


# ---------------------------------------------------------------------------
# stderr hint parsing — BY PATTERN, under real store noise
# ---------------------------------------------------------------------------


def test_hints_parsed_when_store_warnings_precede_them(
    env_save_restore, tmp_path: Path
):
    """The live detector emits a deprecation banner + TOLERATED read-warnings
    ABOVE any payload, and their COUNT varies with the store's contents. So
    hints must be found by the numbered-line pattern, never by position."""
    # Arrange — no stdout JSON at all, forcing the stderr path.
    script = write_detector(
        tmp_path, returncode=2, stderr=hint_block(_ITEMS, with_warnings=True)
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.card_ids == ("sac-card-1", "sac-card-2")


def test_hint_next_actions_survive_the_warning_noise(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(
        tmp_path, returncode=2, stderr=hint_block(_ITEMS, with_warnings=True)
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.items[0].next_action == "Run the failing test and fix it"


def test_warning_lines_are_not_mistaken_for_work_items(
    env_save_restore, tmp_path: Path
):
    # Arrange
    script = write_detector(
        tmp_path, returncode=2, stderr=hint_block(_ITEMS, with_warnings=True)
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert len(verdict.items) == 2


def test_stdout_json_wins_over_stderr_hints(env_save_restore, tmp_path: Path):
    # Arrange — structured payload names one card, hints name two.
    script = write_detector(
        tmp_path,
        returncode=2,
        stdout=runnable_payload([("json-card", "r", "a")]),
        stderr=hint_block(_ITEMS),
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.card_ids == ("json-card",)


# ---------------------------------------------------------------------------
# THE CRITICAL DISTINCTION: unparseable exit 2 is NOT allow
# ---------------------------------------------------------------------------


def test_unparseable_exit_two_stays_runnable(env_save_restore, tmp_path: Path):
    """An exit 2 we merely failed to PARSE must never be downgraded to ALLOW.

    The exit code already proved work exists; losing the detail is our
    problem, not evidence that the board is empty.
    """
    # Arrange
    script = write_detector(
        tmp_path,
        returncode=2,
        stdout="not json at all",
        stderr="no numbered hints here",
    )
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == RUNNABLE


def test_unparseable_exit_two_explains_itself(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(tmp_path, returncode=2, stdout="{{{", stderr="garbage")
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert "exit 2 already proved work exists" in verdict.detail


# ---------------------------------------------------------------------------
# UNKNOWN — we could not tell
# ---------------------------------------------------------------------------


def test_missing_detector_is_unknown_not_allow(env_save_restore, tmp_path: Path):
    """Today's LIVE path: deployed scitex-cards v0.16.1 has no may-stop."""
    # Arrange
    missing_detector(env_save_restore, tmp_path)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_missing_detector_detail_names_the_cause(env_save_restore, tmp_path: Path):
    # Arrange
    missing_detector(env_save_restore, tmp_path)
    # Act
    verdict = probe("a")
    # Assert
    assert "not found" in verdict.detail


def test_unexpected_exit_code_is_unknown(env_save_restore, tmp_path: Path):
    # Arrange — exit 1 is neither "may stop" nor "work remains".
    script = write_detector(tmp_path, returncode=1, stderr="detector blew up")
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_unexpected_exit_code_detail_reports_the_code(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(tmp_path, returncode=9, stderr="boom")
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert "exited 9" in verdict.detail


def test_empty_agent_is_unknown_not_allow(env_save_restore, tmp_path: Path):
    """No identity means we cannot ask the question — not that the answer is
    'nothing to do'. It must not silently become a clean stop."""
    # Arrange
    script = write_detector(tmp_path, returncode=0)
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("")
    # Assert
    assert verdict.state == UNKNOWN


def test_empty_agent_detail_refuses_to_guess_from_cwd(env_save_restore, tmp_path: Path):
    # Arrange
    script = write_detector(tmp_path, returncode=0)
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("")
    # Assert
    assert "working directory" in verdict.detail
