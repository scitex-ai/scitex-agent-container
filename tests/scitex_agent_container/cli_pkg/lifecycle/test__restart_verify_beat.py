"""The beat witness — the runner's own testimony in the restart verdict.

v4 step 5: an SDK agent has no tmux session to ask about
(``instances.screen`` NULL), so the two-witness restart check could only
ever abstain. The runner's incarnation-stamped heartbeat is the second
witness for exactly that case — and because the runner BINDS its
incarnation once at boot (``_runners._incarnation``), an untouched old
process keeps beating its OLD incarnation no matter how many ids the
ledger mints over it, which is what makes the beat evidence rather than
an echo.

Also pins the console rendering fix: a ``None`` (cannot-verify) verdict
renders as CANNOT VERIFY — never as the binary "NOT verified", which
accused restarts nobody could observe.

NO MOCKS: real files under an isolated runtime dir (explicit env
save/restore), the production writers, and the documented injection
seams (``now_fn``/``sleep_fn``) passed real callables.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._restart_local import (
    _NOT_CYCLED,
    _print_local_outcome,
)
from scitex_agent_container.cli_pkg.lifecycle._restart_verify import (
    RestartVerdict,
    read_beat_identity,
    verify_cycled,
)

_RUNTIME_KEY = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"


@pytest.fixture
def runtime_dir(tmp_path: Path, env_save_restore):
    """Isolated runtime root via the shared env fixture.

    ``_runners._session_state`` bakes ``DEFAULT_STATE_ROOT`` from this
    env var at import, so the module is registered for
    reload-after-restore — the documented pattern that keeps the state
    floor intact for the rest of the worker's session.
    """
    import scitex_agent_container._runners._session_state as _session_state

    env_save_restore.set(_RUNTIME_KEY, str(tmp_path))
    env_save_restore.reload_after_restore(_session_state)
    yield tmp_path


def _write_beat(
    runtime_root: Path,
    name: str,
    *,
    incarnation_id: str | None,
    ts: float | None = None,
    pid: int = 777,
) -> Path:
    """Land a beat file exactly as a (separate-process) runner would."""
    state_dir = runtime_root / name
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"ts": ts if ts is not None else time.time(), "pid": pid}
    if incarnation_id is not None:
        payload["incarnation_id"] = incarnation_id
    (state_dir / "heartbeat.json").write_text(json.dumps(payload), encoding="utf-8")
    return state_dir


# ---------------------------------------------------------------------------
# read_beat_identity — observation vs blindness
# ---------------------------------------------------------------------------


def test_no_beat_file_is_blindness(runtime_dir: Path) -> None:
    # Arrange: nothing on disk for this agent.
    name = "ag-nobeat"
    # Act
    seen = read_beat_identity(name)
    # Assert
    assert seen.observed is False


def test_incarnationless_beat_is_blindness(runtime_dir: Path) -> None:
    # Arrange: an observer/proxy beat — no incarnation on it.
    _write_beat(runtime_dir, "ag-proxy", incarnation_id=None)
    # Act
    seen = read_beat_identity("ag-proxy")
    # Assert: the runner's own testimony is not on file.
    assert seen.observed is False


def test_incarnationless_beat_names_the_gap_in_its_reason(runtime_dir: Path) -> None:
    # Arrange
    _write_beat(runtime_dir, "ag-proxy2", incarnation_id=None)
    # Act
    seen = read_beat_identity("ag-proxy2")
    # Assert
    assert "no incarnation_id" in seen.blind_because


def test_incarnation_beat_is_observed_with_its_identity(runtime_dir: Path) -> None:
    # Arrange: a self-testimony beat from the runner process.
    _write_beat(runtime_dir, "ag-self", incarnation_id="inc-42", pid=999)
    # Act
    seen = read_beat_identity("ag-self")
    # Assert
    assert (seen.observed, seen.identity) == (True, "beat:inc-42@pid999")


def test_min_ts_refuses_a_pre_restart_beat(runtime_dir: Path) -> None:
    # Arrange: the latest beat predates the restart — the old runner's
    # testimony must not impersonate the new run.
    _write_beat(runtime_dir, "ag-stale", incarnation_id="inc-old", ts=1000.0)
    # Act
    seen = read_beat_identity("ag-stale", min_ts=2000.0)
    # Assert
    assert seen.observed is False


def test_stale_blindness_cites_the_exit_record(runtime_dir: Path) -> None:
    # Arrange: the old incarnation died and said why; the new one has
    # not spoken. The abstention should carry that WHY to the operator.
    state_dir = _write_beat(
        runtime_dir, "ag-exited", incarnation_id="inc-old", ts=1000.0
    )
    from scitex_agent_container._runners._incarnation import write_exit_record

    write_exit_record(
        state_dir, reason="harness-returned", code=1, incarnation_id="inc-old"
    )
    # Act
    seen = read_beat_identity("ag-exited", min_ts=2000.0)
    # Assert
    assert "harness-returned" in seen.blind_because


def test_wait_polls_until_the_new_beat_lands(runtime_dir: Path) -> None:
    # Arrange: a stale beat now; the "new runner" lands its beat on the
    # second poll (the sleep seam performs the write — real files, a
    # deterministic clock).
    _write_beat(runtime_dir, "ag-poll", incarnation_id="inc-old", ts=1000.0)

    def _sleep(_s: float) -> None:
        _write_beat(runtime_dir, "ag-poll", incarnation_id="inc-new", ts=3000.0)

    # Act
    seen = read_beat_identity(
        "ag-poll",
        min_ts=2000.0,
        wait_s=5.0,
        now_fn=time.time,
        sleep_fn=_sleep,
    )
    # Assert
    assert seen.identity == "beat:inc-new@pid777"


# ---------------------------------------------------------------------------
# verify_cycled — the beat as the second witness, end to end
# ---------------------------------------------------------------------------


def test_a_cycled_sdk_agent_verifies_true_on_beat_witness(runtime_dir: Path) -> None:
    # Arrange: before the restart the old runner beat inc-A; after it, a
    # NEW process beats inc-B and the ledger moved A -> B.
    _write_beat(runtime_dir, "ag-cycle", incarnation_id="inc-A", pid=100)
    before_seen = read_beat_identity("ag-cycle")
    _write_beat(runtime_dir, "ag-cycle", incarnation_id="inc-B", pid=200)
    after_seen = read_beat_identity("ag-cycle")
    # Act
    verdict = verify_cycled(
        "ag-cycle",
        "inc-A",
        "inc-B",
        session_before=before_seen,
        session_after=after_seen,
    )
    # Assert
    assert verdict.verified is True


def test_an_untouched_process_beat_refutes_the_ledger(runtime_dir: Path) -> None:
    # Arrange: the ledger minted a new id (A -> B) but the SAME process
    # kept beating its bound inc-A — the P0 shape, now visible.
    _write_beat(runtime_dir, "ag-echo", incarnation_id="inc-A", pid=100)
    before_seen = read_beat_identity("ag-echo")
    after_seen = read_beat_identity("ag-echo")
    # Act
    verdict = verify_cycled(
        "ag-echo",
        "inc-A",
        "inc-B",
        session_before=before_seen,
        session_after=after_seen,
    )
    # Assert
    assert verdict.verified is False


def test_blind_on_both_witnesses_still_abstains(runtime_dir: Path) -> None:
    # Arrange: no session AND no self-testimony beat — nothing observed.
    name = "ag-blindboth"
    before_seen = read_beat_identity(name)
    after_seen = read_beat_identity(name)
    # Act
    verdict = verify_cycled(
        name,
        "inc-A",
        "inc-B",
        session_before=before_seen,
        session_after=after_seen,
    )
    # Assert: still None — the beat witness never manufactures a pole.
    assert verdict.verified is None


# ---------------------------------------------------------------------------
# _print_local_outcome — TERNARY rendering, not binary
# ---------------------------------------------------------------------------


def test_none_verdict_renders_cannot_verify(capsys) -> None:
    # Arrange: an abstention (no evidence either way).
    verdict = RestartVerdict(None, "no evidence either way", None, None)
    # Act
    _print_local_outcome("ag-r", True, None, verdict)
    # Assert: an abstention is named as one — never the binary label.
    assert "CANNOT VERIFY" in capsys.readouterr().out


def test_none_verdict_never_renders_not_verified(capsys) -> None:
    # Arrange
    verdict = RestartVerdict(None, "no evidence either way", None, None)
    # Act
    _print_local_outcome("ag-r2", True, None, verdict)
    # Assert
    assert "NOT verified" not in capsys.readouterr().out


def test_true_verdict_still_renders_verified(capsys) -> None:
    # Arrange
    verdict = RestartVerdict(True, "cycled, both witnesses agree", "a", "b")
    # Act
    _print_local_outcome("ag-r3", True, None, verdict)
    # Assert
    assert "verified" in capsys.readouterr().out


def test_refuted_cycle_renders_the_verdict_reason(capsys) -> None:
    # Arrange: the postcondition refuted the cycle (restarted=False).
    verdict = RestartVerdict(False, "still the same run", "a", "a")
    # Act
    _print_local_outcome("ag-r4", False, _NOT_CYCLED, verdict)
    # Assert
    assert "still the same run" in capsys.readouterr().out
