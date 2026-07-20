"""End-to-end tests for ``sac never-stop-when-task-remains``.

PA-306 no-mocks. Every test drives the real Click command with a real
Stop-hook payload on stdin, against a REAL executable spawned as a real
subprocess, with real on-disk loop-guard state. Assertions are made on the
JSON the hook writes to stdout — the exact bytes Claude Code parses as its
Stop decision.

Contract (Claude Code hooks reference, "Stop decision control"): exit 0 with
``{"decision": "block", "reason": ...}`` prevents the stop and feeds
``reason`` back as the agent's next instruction; exit 0 with no stdout
allows the stop.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container._never_stop_when_task_remains._loop_guard import (
    MAX_CONSECUTIVE_BLOCKS,
)
from scitex_agent_container.cli_pkg.never_stop_when_task_remains_cmds import (
    never_stop_when_task_remains,
)

from .._never_stop_when_task_remains._fake_detector import (
    clear_identity,
    detector_env,
    isolate_runtime,
    missing_detector,
    runnable_verdict,
    stale_cards_detector,
    write_detector,
)

_REASON = "Do NOT stop — take card-1 next: run the failing test and fix it."
_HOOK_JSON = json.dumps({"decision": "block", "reason": _REASON})

#: A real Stop-hook payload, shaped as the docs specify.
_PAYLOAD = json.dumps(
    {
        "session_id": "abc123",
        "transcript_path": "/home/agent/.claude/projects/x/t.jsonl",
        "cwd": "/work",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "Done for now.",
    }
)


def _run(agent: str = "") -> "tuple[int, str]":
    args = ["--agent", agent] if agent else []
    result = CliRunner().invoke(never_stop_when_task_remains, args, input=_PAYLOAD)
    return result.exit_code, result.stdout


def _decision(stdout: str) -> dict:
    return json.loads(stdout) if stdout.strip() else {}


# ---------------------------------------------------------------------------
# allow
# ---------------------------------------------------------------------------


def test_exit_zero_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") is None


def test_allowed_stop_exits_zero(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    code, _ = _run("agent-x")
    # Assert
    assert code == 0


def test_allowed_stop_writes_no_stdout(env_save_restore, tmp_path: Path):
    """stdout IS the protocol — an allow must emit nothing at all."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    _, out = _run("agent-x")
    # Assert
    assert out.strip() == ""


# ---------------------------------------------------------------------------
# block — their decision, delivered
# ---------------------------------------------------------------------------


def test_hook_json_block_reaches_stdout(env_save_restore, tmp_path: Path):
    """THE core invariant: a stop attempted with runnable work is blocked.

    Mutation-proved — removing the block from ``_decide.decide`` turns this
    test RED.
    """
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


def test_their_reason_is_delivered_verbatim(env_save_restore, tmp_path: Path):
    """Refusing the stop is NOT enough — a refused stop leaves the agent
    idle. Their reason becomes the agent's next instruction, unmodified."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("reason") == _REASON


def test_unknown_payload_fields_reach_claude_code(env_save_restore, tmp_path: Path):
    """scitex-cards must be able to evolve their payload without a sac
    release, so fields sac has never heard of pass straight through."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    payload = json.dumps(
        {"decision": "block", "reason": _REASON, "someFutureField": [1, 2]}
    )
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=payload)
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("someFutureField") == [1, 2]


def test_a_real_runnable_verdict_blocks_the_stop(env_save_restore, tmp_path: Path):
    """THE CONTROL for the fix below.

    Uses the shape ``may-stop`` ACTUALLY emits — its own verdict schema,
    carrying ``runnable``/``items`` and no ``decision`` key. The suite
    previously exercised only hook-protocol JSON, which the detector never
    sends, so this path was entirely untested in the direction that matters.
    Without this test, "never block at all" would pass as a fix.
    """
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_verdict()),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


def test_block_reason_is_composed_from_their_verdict(env_save_restore, tmp_path: Path):
    """Refusing the stop is NOT enough — a refused stop leaves the agent
    idle. The instruction must name the work, in the detector's own words."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_verdict(
                items=[{"card_id": "card-42", "next_action": "land the fix"}]
            ),
        ),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert "card-42" in _decision(out)["reason"]


def test_block_reason_is_never_empty(env_save_restore, tmp_path: Path):
    """The docs require ``reason`` whenever ``decision`` is ``block``."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_verdict(items=[])),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("reason", "").strip()


# ---------------------------------------------------------------------------
# THE LIVE DEFECT — a host whose scitex-cards predates `may-stop`
#
# `may-stop` shipped in scitex-cards 0.16.2; the fleet's SIF baked 0.16.1, so
# this is the STEADY STATE, not an edge case. Click exits 2 for a usage
# error, which is the same code the protocol uses for "work remains", so a
# failure to answer was being read as an affirmative "you may NOT stop" — and
# the usage text was handed to the agent as its next instruction.
# ---------------------------------------------------------------------------


def test_stale_cards_usage_error_allows_the_stop(env_save_restore, tmp_path: Path):
    """ "I could not tell" must never be served as "you may NOT stop"."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    _, out = _run("dotfiles")
    # Assert
    assert _decision(out).get("decision") is None


def test_stale_cards_usage_error_emits_no_reason(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    _, out = _run("dotfiles")
    # Assert
    assert "reason" not in _decision(out)


def test_stale_cards_usage_text_never_reaches_the_agent(
    env_save_restore, tmp_path: Path
):
    """The exact strings from the reported artifact, asserted on the exact
    bytes the hook writes — the only thing Claude Code actually reads."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    _, out = _run("dotfiles")
    # Assert
    assert "No such command" not in out and "Usage:" not in out


def test_stale_cards_fail_open_is_loud(env_save_restore, tmp_path: Path):
    """A silent allow is indistinguishable from a clean board — which is
    exactly where a fleet-wide breakage would hide."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    _, out = _run("dotfiles")
    # Assert
    assert "FAIL-OPEN" in _decision(out).get("systemMessage", "")


def test_stale_cards_message_names_the_actionable_cause(
    env_save_restore, tmp_path: Path
):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, stale_cards_detector(tmp_path))
    # Act
    _, out = _run("dotfiles")
    # Assert
    assert "predates the `may-stop` verb" in _decision(out).get("systemMessage", "")


# ---------------------------------------------------------------------------
# fail-open
# ---------------------------------------------------------------------------


def test_missing_executable_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    missing_detector(env_save_restore, tmp_path)
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") is None


def test_missing_executable_logs_loudly(env_save_restore, tmp_path: Path):
    """Fail-open must be VISIBLE — a silent allow is indistinguishable from
    a clean board, which is exactly where the incident hid."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    missing_detector(env_save_restore, tmp_path)
    # Act
    _, out = _run("agent-x")
    # Assert
    assert "FAIL-OPEN" in _decision(out).get("systemMessage", "")


def test_crashing_executable_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=1, stderr="Traceback ... boom"),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") is None


def test_crashing_executable_reports_the_exit_code(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=7, stderr="boom")
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert "exited 7" in _decision(out).get("systemMessage", "")


def test_unresolvable_identity_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    clear_identity(env_save_restore)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2))
    # Act
    _, out = _run()
    # Assert
    assert _decision(out).get("decision") is None


def test_unresolvable_identity_says_so_loudly(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    clear_identity(env_save_restore)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=2))
    # Act
    _, out = _run()
    # Assert
    assert "identity" in _decision(out).get("systemMessage", "")


def test_identity_comes_from_env_not_cwd(env_save_restore, tmp_path: Path):
    """The hook resolves WHO it is from the agent's own environment."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    clear_identity(env_save_restore)
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "env-resolved-agent")
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    code, out = _run()
    # Assert
    assert _decision(out).get("decision") == "block"


# ---------------------------------------------------------------------------
# loop guard
# ---------------------------------------------------------------------------


def _block_repeatedly(times: int) -> "tuple[int, str]":
    last: "tuple[int, str]" = (0, "")
    for _ in range(times):
        last = _run("agent-x")
    return last


def test_repeated_identical_blocks_eventually_allow_the_stop(
    env_save_restore, tmp_path: Path
):
    """An agent that can never end its turn is a worse failure than an idle
    one, so after N unproductive blocks the guard yields."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS + 1)
    # Assert
    assert _decision(out).get("decision") is None


def test_loop_guard_alarms_when_it_trips(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS + 1)
    # Assert
    assert "ALARM" in _decision(out).get("systemMessage", "")


def test_guard_still_blocks_up_to_the_limit(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore, write_detector(tmp_path, returncode=2, stdout=_HOOK_JSON)
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS)
    # Assert
    assert _decision(out).get("decision") == "block"


def test_an_allowed_stop_clears_the_block_history(env_save_restore, tmp_path: Path):
    """A clean stop resets the counter, so a later block starts from 1."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    blocking = write_detector(
        tmp_path, returncode=2, stdout=_HOOK_JSON, name="blocking"
    )
    allowing = write_detector(tmp_path, returncode=0, name="allowing")
    detector_env(env_save_restore, blocking)
    _block_repeatedly(MAX_CONSECUTIVE_BLOCKS)
    detector_env(env_save_restore, allowing)
    _run("agent-x")
    # Act
    detector_env(env_save_restore, blocking)
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


def test_changed_reason_is_progress_and_keeps_blocking(
    env_save_restore, tmp_path: Path
):
    """Progress resets the counter — an agent working through a queue must
    never be alarmed just for having a long queue."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    out = ""
    for idx in range(MAX_CONSECUTIVE_BLOCKS + 2):
        payload = json.dumps({"decision": "block", "reason": f"take card-{idx}"})
        detector_env(
            env_save_restore,
            write_detector(tmp_path, returncode=2, stdout=payload, name=f"det-{idx}"),
        )
        # Act
        _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


# ---------------------------------------------------------------------------
# stdout hygiene
# ---------------------------------------------------------------------------


def test_stdout_is_pure_json_when_blocking(env_save_restore, tmp_path: Path):
    """The docs require stdout to contain ONLY the JSON object — a stray
    diagnostic line makes Claude Code fail to parse the decision, silently
    no-opping the whole feature."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stdout=_HOOK_JSON, stderr="noise on stderr"
        ),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert json.loads(out)["decision"] == "block"
