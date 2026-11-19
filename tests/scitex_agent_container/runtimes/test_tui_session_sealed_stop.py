"""A sealed Claude/Codex/OpenCode pane's outcome is finalized even when the
pane teardown is slow or fails (the Hermes fix, for the generic TUI stop).

Real ``TuiSessionRuntime`` + real ``end_run``; the only stand-ins are a
multiplexer whose teardown check is SLOW and then fails (the shape a loaded
host produced) and no-op inbound owners (no host pollers in a unit test).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._sandbox._run import end_run
from scitex_agent_container._sandbox._spec import parse_sandbox
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes.tui_session import (
    TuiSessionRuntime,
    TuiStopVerificationError,
    state_dir_for_config,
)


class _SlowFailingMux:
    """tmux whose teardown verification takes long and then fails."""

    def exists(self, name: str) -> bool:
        return True

    def stop(self, name: str) -> bool:
        time.sleep(0.3)
        return False


class _QuietTui(TuiSessionRuntime):
    def _owns_external_cct_poller(self, config) -> bool:
        return False

    def _stop_inbox(self, config) -> None:
        return None


@pytest.fixture(params=["claude-code", "codex", "opencode"])
def stopped(tmp_path, request):
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": [], "model_key": "K"},
                "run": {"root": "/scratch/unused", "size": "1G", "after": "remove"},
            }
        }
    )
    cfg = AgentConfig(
        name=f"sealed-gstop-{request.param}-{tmp_path.name}",
        runtime="tui",
        harness=request.param,
        sandbox=sb,
    )
    state = state_dir_for_config(cfg)
    run = tmp_path / "run"
    for d in ("home", "overlay", "out", "secrets"):
        (run / d).mkdir(parents=True)
    (run / "out" / "outcome.json").write_text(
        json.dumps({"status": "running", "policy": {"after": "remove"}})
    )
    (state / "sandbox").mkdir(parents=True, exist_ok=True)
    link = state / "sandbox" / "run"
    link.unlink(missing_ok=True)
    link.symlink_to(run)
    rt = _QuietTui(multiplexer=_SlowFailingMux(), turn_bridge_stop=lambda c: None)
    with pytest.raises(TuiStopVerificationError):
        rt.stop(cfg)
    yield cfg, state, run
    link.unlink(missing_ok=True)


def _outcome(run: Path) -> dict:
    return json.loads((run / "out" / "outcome.json").read_text())


def test_failed_generic_teardown_still_finalizes_the_outcome(stopped):
    # Arrange
    _, _, run = stopped
    # Act
    rec = _outcome(run)
    # Assert
    assert rec["status"] == "finalized"


def test_failed_generic_teardown_attributes_the_stop_to_sac(stopped):
    # Arrange
    _, _, run = stopped
    # Act
    exit_doc = _outcome(run)["exit"]
    # Assert
    assert exit_doc["stopped_by"] == "sac"


def test_failed_generic_teardown_keeps_the_layers_deferred(stopped):
    # Arrange
    _, _, run = stopped
    # Act
    layers = _outcome(run)["layers"]
    # Assert
    assert "deferred" in layers and (run / "home").is_dir()


def test_later_verified_generic_stop_completes_the_removal(stopped):
    # Arrange
    cfg, state, run = stopped
    # Act
    end_run(cfg, state, teardown_verified=True)
    # Assert
    assert not (run / "home").exists() and (run / "out" / "outcome.json").is_file()
