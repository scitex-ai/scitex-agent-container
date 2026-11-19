"""Per-run lifecycle: fresh run dir + $HOME per start, finalize on stop.

Runs the real ``begin_run`` / ``end_run``. The run root is
``tmp_path`` only when it is not under /tmp — the sandbox refuses tmp roots —
so the root is taken from ``SAC_SANDBOX_TEST_ROOT`` when set, else these
tests are skipped.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._sandbox._layout import RunPaths
from scitex_agent_container._sandbox._outcome import atomic_write_json, read_json
from scitex_agent_container._sandbox._run import (
    begin_run,
    current_run,
    end_run,
    finalize_run,
    prepare_run_dir,
)
from scitex_agent_container._sandbox._spec import parse_sandbox

ROOT = os.environ.get("SAC_SANDBOX_TEST_ROOT", "")
pytestmark = pytest.mark.skipif(
    not ROOT,
    reason="needs SAC_SANDBOX_TEST_ROOT (a non-tmp scratch dir)",
)


@pytest.fixture
def cfg_and_state(tmp_path):
    root = Path(ROOT) / f"unit-{os.getpid()}-{tmp_path.name}"
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": [], "model_key": "K"},
                "run": {"root": str(root / "runs"), "size": "64M", "after": "remove"},
            }
        }
    )
    cfg = SimpleNamespace(name="u", sandbox=sb, apptainer=None, hermes_failover=None)
    yield cfg, root / "state"
    shutil.rmtree(root, ignore_errors=True)


def test_home_points_into_the_fresh_run(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    # Act
    run_dir = begin_run(cfg, state)
    # Assert
    assert (state / "home").resolve() == (run_dir / "home").resolve()


def test_preexisting_home_is_moved_aside_not_deleted(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    (state / "home").mkdir(parents=True)
    (state / "home" / "keep.txt").write_text("x")
    # Act
    begin_run(cfg, state)
    # Assert
    assert [p.name for p in state.glob("home.pre-sandbox-*/keep.txt")] == ["keep.txt"]


def test_second_start_gets_a_new_empty_home(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    first = begin_run(cfg, state)
    (first / "home" / "leftover").write_text("x")
    # Act
    second = begin_run(cfg, state)
    # Assert
    assert second != first and not (state / "home" / "leftover").exists()


def test_never_launched_run_is_finalized_with_reason(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    begin_run(cfg, state)
    # Act
    rec = end_run(cfg, state)
    # Assert
    assert rec["exit"]["code"] is None and "never launched" in rec["exit"]["reason"]


def test_killed_supervisor_leaves_a_stated_null_exit(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    rp = RunPaths(begin_run(cfg, state))
    rec = read_json(rp.outcome)
    rec["status"] = "running"
    atomic_write_json(rp.outcome, rec)
    # Act
    final = end_run(cfg, state)
    # Assert
    assert (
        final["exit"]["code"] is None and "supervisor ended" in final["exit"]["reason"]
    )


def test_remove_keeps_only_the_out_dir(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    run_dir = begin_run(cfg, state)
    # Act
    end_run(cfg, state)
    # Assert
    assert sorted(p.name for p in run_dir.iterdir()) == ["out"]


def test_end_run_is_idempotent(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    begin_run(cfg, state)
    first = end_run(cfg, state)
    # Act
    second = end_run(cfg, state)
    # Assert
    assert second == first


def test_current_run_follows_the_link(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    run_dir = begin_run(cfg, state)
    # Act
    got = current_run(state)
    # Assert
    assert got == run_dir


# Plain-command runs (sac sandbox exec): a run dir with no state-dir links.


def test_prepared_command_run_touches_no_state_dir(cfg_and_state):
    # Arrange
    cfg, state = cfg_and_state
    # Act
    prepare_run_dir(cfg.sandbox, "exec", record={"kind": "command"})
    # Assert
    assert not state.exists()


def test_prepared_command_run_records_its_kind(cfg_and_state):
    # Arrange
    cfg, _ = cfg_and_state
    # Act
    run_dir = prepare_run_dir(cfg.sandbox, "exec", record={"kind": "command"})
    # Assert
    assert read_json(RunPaths(run_dir).outcome)["kind"] == "command"


def test_finalized_command_run_claims_no_harness_usage(cfg_and_state):
    # Arrange
    cfg, _ = cfg_and_state
    rp = RunPaths(prepare_run_dir(cfg.sandbox, "exec", record={"kind": "command"}))
    # Act
    rec = finalize_run(rp)
    # Assert
    assert "token_usage" not in rec and "tool_calls" not in rec
