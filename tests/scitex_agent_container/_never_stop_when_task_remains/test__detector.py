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
    MIN_CARDS_VERSION,
    UNKNOWN,
    detector_argv,
    probe,
)

from ._fake_detector import (
    detector_env,
    missing_detector,
    runnable_verdict,
    stale_cards_detector,
    write_detector,
)

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
# exit 2 — admissible ONLY with a parseable verdict alongside it
#
# Exit 2 means "work remains" AND is the universal CLI usage-error code, so
# an exit code on its own cannot be read as an affirmative answer.
# ---------------------------------------------------------------------------


def test_exit_zero_is_allow(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == ALLOW


def test_exit_two_with_a_runnable_verdict_blocks(env_save_restore, tmp_path: Path):
    """THE CONTROL. Without it, "never block" would pass as a fix."""
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_verdict()),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == BLOCK


def test_block_reason_is_composed_from_the_detectors_payload(
    env_save_restore, tmp_path: Path
):
    """The instruction handed back must be the DETECTOR'S words."""
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_verdict(
                items=[{"card_id": "card-7", "next_action": "ship the fix"}]
            ),
        ),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert "card-7" in verdict.payload["reason"]


def test_block_reason_omits_volatile_fields(env_save_restore, tmp_path: Path):
    """``idle_seconds`` moves every turn; if it reached the reason, the loop
    guard's signature would change every turn and the guard could never
    trip."""
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stdout=runnable_verdict(idle_seconds=4_242)
        ),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert "4242" not in verdict.payload["reason"]


def test_exit_two_saying_not_runnable_is_allow(env_save_restore, tmp_path: Path):
    # Arrange — a parseable answer, and the answer is "nothing runnable".
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stdout=json.dumps({"runnable": False, "items": []})
        ),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == ALLOW


# ---------------------------------------------------------------------------
# THE LIVE DEFECT — a scitex-cards older than `may-stop`
#
# `may-stop` shipped in scitex-cards 0.16.2 and the SIF baked 0.16.1, so this
# is the fleet's STEADY STATE, not an edge case. Every one of these went the
# other way before the fix: rc=2 was consumed as an affirmative BLOCK and the
# usage text became the agent's next instruction.
# ---------------------------------------------------------------------------


def test_missing_verb_usage_error_is_unknown_not_block(
    env_save_restore, tmp_path: Path
):
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_missing_verb_usage_error_never_becomes_a_reason(
    env_save_restore, tmp_path: Path
):
    """A usage error must never be handed to the agent as an instruction."""
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert "No such command" not in verdict.reason


def test_missing_verb_produces_no_block_payload(env_save_restore, tmp_path: Path):
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.payload is None


def test_missing_verb_detail_names_the_version_cause(env_save_restore, tmp_path: Path):
    """The loud log must say WHY we could not be consulted."""
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert "predates the `may-stop` verb" in verdict.detail


def test_exit_two_with_unparseable_stdout_is_unknown(env_save_restore, tmp_path: Path):
    # Arrange — output arrived, but it is not a verdict we can read.
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout="not json at all"),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_exit_two_with_json_lacking_the_verdict_key_is_unknown(
    env_save_restore, tmp_path: Path
):
    # Arrange — valid JSON, but it answers a different question.
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=json.dumps({"hello": "world"})),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


def test_exit_two_with_silent_streams_is_unknown(env_save_restore, tmp_path: Path):
    """Nothing on either stream is not an answer, it is a failure to answer."""
    # Arrange
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2))
    # Act
    verdict = probe("a")
    # Assert
    assert verdict.state == UNKNOWN


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


# ---------------------------------------------------------------------------
# WHICH SIDE IS STALE? — provenance on every failure path
#
# "No such command 'may-stop'" names the verb but never the VERSION that
# lacks it, so it reads identically whether the caller invented a verb or the
# callee is too old to have it. Three agents investigated that one string
# before anyone established which side was stale — and the one who guessed,
# guessed wrong and proposed changing the (correct) caller. These assertions
# make the log answer that question by itself.
# ---------------------------------------------------------------------------


def test_failure_detail_names_the_resolved_binary(env_save_restore, tmp_path: Path):
    # Arrange
    script = stale_cards_detector(tmp_path)
    detector_env(env_save_restore, script)
    # Act
    verdict = probe("a")
    # Assert
    assert str(script) in verdict.detail


def test_failure_detail_names_the_argv_actually_executed(
    env_save_restore, tmp_path: Path
):
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert "--agent a" in verdict.detail


def test_failure_detail_reports_the_version_the_binary_claims(
    env_save_restore, tmp_path: Path
):
    # Arrange — a CLI that answers `--version` the ordinary way.
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout="scitex-cards, version 0.16.2"),
    )
    # Act
    verdict = probe("a")
    # Assert
    assert "reports version: 0.16.2" in verdict.detail


def test_version_probe_never_leaks_cli_prose(env_save_restore, tmp_path: Path):
    """A CLI too old to answer ``--version`` cleanly must not smuggle its
    usage text into a message that reaches the agent. Only a version-SHAPED
    token is ever extracted."""
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert "reports version: unknown" in verdict.detail


def test_missing_executable_detail_says_it_resolved_nowhere(
    env_save_restore, tmp_path: Path
):
    # Arrange
    missing_detector(env_save_restore, tmp_path)
    # Act
    verdict = probe("a")
    # Assert
    assert "NOT FOUND on PATH" in verdict.detail


def test_failure_detail_states_the_compatibility_floor(
    env_save_restore, tmp_path: Path
):
    """The skew should be STATED, not discovered by an agent that cannot
    stop."""
    # Arrange
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    verdict = probe("a")
    # Assert
    assert MIN_CARDS_VERSION in verdict.detail


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
        write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_verdict(items=[{"card_id": "card-a"}]),
            name="d1",
        ),
    )
    first = probe("a").block_signature_source()
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_verdict(items=[{"card_id": "card-b"}]),
            name="d2",
        ),
    )
    # Act
    second = probe("a").block_signature_source()
    # Assert
    assert first != second


def test_signature_source_is_stable_while_only_idle_seconds_moves(
    env_save_restore, tmp_path: Path
):
    """Same work, later turn — the guard must still see a repeat."""
    # Arrange
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stdout=runnable_verdict(idle_seconds=10), name="e1"
        ),
    )
    first = probe("a").block_signature_source()
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_verdict(idle_seconds=9_999),
            name="e2",
        ),
    )
    # Act
    second = probe("a").block_signature_source()
    # Assert
    assert first == second


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
