"""End-to-end tests for ``sac take-next-item`` — the never-stop Stop hook.

PA-306 no-mocks. Every test drives the real Click command with a real Stop-
hook payload on stdin, against a REAL detector executable spawned as a real
subprocess, with real on-disk loop-guard state. The assertions are made on
the JSON the hook writes to stdout — i.e. on the exact bytes Claude Code
parses as its Stop decision.

The contract under test (Claude Code hooks reference, "Stop decision
control"): exit 0 with ``{"decision": "block", "reason": ...}`` prevents the
stop and feeds ``reason`` back as the agent's next instruction; exit 0 with
no stdout allows the stop.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container._never_stop._loop_guard import MAX_CONSECUTIVE_BLOCKS
from scitex_agent_container.cli_pkg.never_stop_cmds import take_next_item

from .._never_stop._fake_detector import (
    clear_identity,
    detector_env,
    hint_block,
    isolate_runtime,
    missing_detector,
    runnable_payload,
    write_detector,
)

_ITEMS = [
    ("sac-card-1", "in_progress, untouched 3h", "Run the failing test and fix it"),
    ("sac-card-2", "unread inbox", "Poll your inbox and act on the digest"),
]

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
    result = CliRunner().invoke(take_next_item, args, input=_PAYLOAD)
    return result.exit_code, result.stdout


def _decision(stdout: str) -> dict:
    return json.loads(stdout) if stdout.strip() else {}


# ---------------------------------------------------------------------------
# exit 0 → the stop is ALLOWED
# ---------------------------------------------------------------------------


def test_exit_zero_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(env_save_restore, write_detector(tmp_path, returncode=0))
    # Act
    code, out = _run("agent-x")
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
# exit 2 → the stop is BLOCKED and CONVERTED into the next item
# ---------------------------------------------------------------------------


def test_exit_two_blocks_the_stop(env_save_restore, tmp_path: Path):
    """THE core invariant: a stop attempted with runnable work is blocked.

    Mutation-proved — removing the block from ``_decide.decide`` turns this
    test RED.
    """
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


def test_block_carries_the_parsed_next_actions(env_save_restore, tmp_path: Path):
    """Refusing the stop is NOT enough — a refused stop leaves the agent
    idle. The continuation must hand it the actual next action."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run("agent-x")
    reason = _decision(out).get("reason", "")
    # Assert
    assert "Run the failing test and fix it" in reason


def test_block_carries_every_next_action(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run("agent-x")
    reason = _decision(out).get("reason", "")
    # Assert
    assert all(action in reason for _, _, action in _ITEMS)


def test_block_names_the_cards(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run("agent-x")
    reason = _decision(out).get("reason", "")
    # Assert
    assert "sac-card-1" in reason and "sac-card-2" in reason


def test_block_reason_is_non_empty(env_save_restore, tmp_path: Path):
    """The docs require ``reason`` whenever ``decision`` is ``block``."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("reason", "").strip()


def test_block_works_from_stderr_hints_under_store_warnings(
    env_save_restore, tmp_path: Path
):
    # Arrange — no stdout JSON; hints sit below the real warning noise.
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(
            tmp_path, returncode=2, stderr=hint_block(_ITEMS, with_warnings=True)
        ),
    )
    # Act
    _, out = _run("agent-x")
    reason = _decision(out).get("reason", "")
    # Assert
    assert "Poll your inbox and act on the digest" in reason


def test_unparseable_exit_two_still_blocks(env_save_restore, tmp_path: Path):
    """ "We could not parse it" must not become "nothing to do"."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout="garbage", stderr="noise"),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") == "block"


# ---------------------------------------------------------------------------
# fail-open — a broken detector must never wedge the agent
# ---------------------------------------------------------------------------


def test_missing_detector_allows_the_stop(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    missing_detector(env_save_restore, tmp_path)
    # Act
    _, out = _run("agent-x")
    # Assert
    assert _decision(out).get("decision") is None


def test_missing_detector_logs_loudly(env_save_restore, tmp_path: Path):
    """Fail-open must be VISIBLE — a silent allow is indistinguishable from
    a clean board, which is exactly the state the incident hid in."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    missing_detector(env_save_restore, tmp_path)
    # Act
    _, out = _run("agent-x")
    # Assert
    assert "FAIL-OPEN" in _decision(out).get("systemMessage", "")


def test_crashing_detector_allows_the_stop(env_save_restore, tmp_path: Path):
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


def test_crashing_detector_reports_the_exit_code(env_save_restore, tmp_path: Path):
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
    """The hook must resolve WHO it is from the agent's own environment."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    clear_identity(env_save_restore)
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "env-resolved-agent")
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _run()
    # Assert
    assert "env-resolved-agent" in _decision(out).get("reason", "")


# ---------------------------------------------------------------------------
# loop guard — alarm instead of re-driving forever
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
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS + 1)
    # Assert
    assert _decision(out).get("decision") is None


def test_loop_guard_alarms_when_it_trips(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS + 1)
    # Assert
    assert "ALARM" in _decision(out).get("systemMessage", "")


def test_alarm_names_the_stuck_cards(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
    )
    # Act
    _, out = _block_repeatedly(MAX_CONSECUTIVE_BLOCKS + 1)
    # Assert
    assert "sac-card-1" in _decision(out).get("systemMessage", "")


def test_guard_still_blocks_up_to_the_limit(env_save_restore, tmp_path: Path):
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    detector_env(
        env_save_restore,
        write_detector(tmp_path, returncode=2, stdout=runnable_payload(_ITEMS)),
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
        tmp_path, returncode=2, stdout=runnable_payload(_ITEMS), name="blocking"
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


def test_changed_work_set_is_progress_and_keeps_blocking(
    env_save_restore, tmp_path: Path
):
    """Progress resets the counter — an agent working through a queue must
    never be alarmed just for having a long queue."""
    # Arrange
    isolate_runtime(env_save_restore, tmp_path)
    for idx in range(MAX_CONSECUTIVE_BLOCKS + 2):
        script = write_detector(
            tmp_path,
            returncode=2,
            stdout=runnable_payload([(f"card-{idx}", "r", f"do thing {idx}")]),
            name=f"det-{idx}",
        )
        detector_env(env_save_restore, script)
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
            tmp_path,
            returncode=2,
            stdout=runnable_payload(_ITEMS),
            stderr=hint_block(_ITEMS, with_warnings=True),
        ),
    )
    # Act
    _, out = _run("agent-x")
    # Assert
    assert json.loads(out)["decision"] == "block"
