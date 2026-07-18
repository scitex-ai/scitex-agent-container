"""Tests for ``_never_stop_when_task_remains._detector`` — classify, don't parse.

PA-306 no-mocks: every probe spawns a REAL executable written to
``tmp_path`` that prints real streams and exits with a real code, reached
through the production ``$SAC_MAY_STOP_CMD`` knob.

What these tests assert is that sac classifies the RESULT and forwards the
executable's decision untouched. What they deliberately do NOT assert is
anything about scitex-cards' payload schema — no ``items[]``, no
``card_id``, no hint-line shape. Those assertions would re-establish the
coupling this boundary removed.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._never_stop_when_task_remains._detector import (
    ALLOW,
    BLOCK,
    UNKNOWN,
    detector_argv,
    probe,
)

from ._fake_detector import detector_env, missing_detector, write_detector

#: A hook-protocol payload as the executable would emit it. The nested field
#: names here belong to the CLAUDE CODE Stop-hook contract, not to
#: scitex-cards' schema — sac reads `decision` only, and forwards the rest.
_HOOK_JSON = json.dumps(
    {"decision": "block", "reason": "Do NOT stop — take card-1 next."}
)


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_default_argv_calls_the_cards_executable(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_MAY_STOP_CMD")
    # Act
    argv = detector_argv("scitex-hub")
    # Assert
    assert argv == ["scitex-cards", "may-stop", "--agent", "scitex-hub"]


def test_argv_always_names_the_agent(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_MAY_STOP_CMD", "/usr/bin/custom hook --flag")
    # Act
    argv = detector_argv("worker-7")
    # Assert
    assert argv[-2:] == ["--agent", "worker-7"]


# ---------------------------------------------------------------------------
# hook-protocol passthrough — their decision, forwarded verbatim
# ---------------------------------------------------------------------------


def test_hook_json_block_is_classified_as_block(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == BLOCK


def test_hook_json_payload_is_forwarded_untouched(env_save_restore, tmp_path: Path):
    """sac must not rewrite, re-render, or drop fields from their payload."""
    # Arrange
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.payload == json.loads(_HOOK_JSON)


def test_unknown_payload_fields_survive(env_save_restore, tmp_path: Path):
    """A field sac has never heard of must reach Claude Code intact — that is
    what lets scitex-cards evolve their output without a sac release."""
    # Arrange
    payload = json.dumps(
        {"decision": "block", "reason": "r", "someFutureField": {"nested": 1}}
    )
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=payload)
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.payload["someFutureField"] == {"nested": 1}


def test_hook_json_without_block_is_allow(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=0, stdout='{"systemMessage":"fyi"}'),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == ALLOW


def test_hook_json_found_below_a_noisy_stdout_line(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stdout="warning: something\n" + _HOOK_JSON
        ),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == BLOCK


# ---------------------------------------------------------------------------
# transitional exit-code path — stderr forwarded as OPAQUE text
# ---------------------------------------------------------------------------


def test_exit_zero_is_allow(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == ALLOW


def test_exit_two_is_block(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stderr="work remains, do X"),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == BLOCK


def test_exit_two_forwards_stderr_verbatim(env_save_restore, tmp_path: Path):
    """Their text is forwarded, not interpreted. sac must not reformat it,
    strip lines from it, or extract fields out of it."""
    # Arrange
    text = "1. card-a — reason — do the thing\n2. card-b — reason — do the other"
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2, stderr=text))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.reason == text


def test_exit_two_with_silent_stderr_still_blocks(env_save_restore, tmp_path: Path):
    """Exit 2 already proved work remains; having nothing to quote does not
    downgrade that to 'nothing to do'."""
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == BLOCK


def test_exit_two_with_silent_stderr_supplies_a_reason(
    env_save_restore, tmp_path: Path
):
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2))
    # Act
    verdict = probe("a")
    # Assert
    assert "Do not stop" in verdict.reason


# ---------------------------------------------------------------------------
# UNKNOWN — we could not tell
# ---------------------------------------------------------------------------


def test_missing_executable_is_unknown_not_allow(env_save_restore, tmp_path: Path):
    # Arrange
    missing_detector(env_save_restore, tmp_path)
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_missing_executable_detail_names_the_cause(env_save_restore, tmp_path: Path):
    # Arrange
    missing_detector(env_save_restore, tmp_path)
    # Act
    verdict = probe("a")
    # Assert
    assert "not found" in verdict.detail


def test_unexpected_exit_code_is_unknown(env_save_restore, tmp_path: Path):
    # Arrange — exit 1 is neither "may stop" nor "work remains".
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=1, stderr="blew up")
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_unexpected_exit_code_detail_reports_the_code(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=9, stderr="boom")
    )
    # Act
    verdict = probe("a")
    # Assert
    assert "exited 9" in verdict.detail


def test_empty_agent_is_unknown_not_allow(env_save_restore, tmp_path: Path):
    """No identity means we cannot ask the question — not that the answer is
    'nothing to do'."""
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    verdict = probe("")
    # Assert
    assert verdict.state == UNKNOWN


def test_empty_agent_detail_refuses_to_guess_from_cwd(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    verdict = probe("")
    # Assert
    assert "working directory" in verdict.detail


# ---------------------------------------------------------------------------
# the loop-guard signature source stays opaque
# ---------------------------------------------------------------------------


def test_signature_source_differs_when_their_text_changes(
    env_save_restore, tmp_path: Path
):
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stderr="first", name="d1"),
    )
    first = probe("a").block_signature_source()
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stderr="second", name="d2"),
    )
    # Act
    second = probe("a").block_signature_source()
    # Assert
    assert first != second


def test_signature_source_is_stable_for_identical_output(
    env_save_restore, tmp_path: Path
):
    # Arrange
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    first = probe("a").block_signature_source()
    # Act
    second = probe("a").block_signature_source()
    # Assert
    assert first == second
