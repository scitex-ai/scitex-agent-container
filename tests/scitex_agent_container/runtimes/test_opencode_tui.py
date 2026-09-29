"""Tests for ``runtimes/opencode_tui.py``.

The gateway is a recording fake; the multiplexer is never touched
(start/stop stay covered by the ``TuiSessionRuntime`` suite). State
lands under a fake ``$HOME`` plus a spec path outside any project
scope, so the real ``state_dir_for_config`` resolves into ``tmp_path``
— the ``test_info_cmds`` HOME-seam pattern, no patches.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._gateway_harness import GatewayHarnessError
from scitex_agent_container.runtimes.opencode_tui import OpencodeTuiSessionRuntime


class _Gateway:
    """Recording gateway double (a real Protocol impl, no mocks)."""

    def __init__(self, states=None, error=None):
        self.states = dict(states or {})
        self.error = error
        self.submits = []

    def session_states(self, state_dir, timeout_s=10.0):
        if self.error is not None:
            raise self.error
        return dict(self.states)

    def submit_turn(
        self, state_dir, name, text, delivery_mode="steer", options=None, **kwargs
    ):
        self.submits.append((name, text, delivery_mode, options))
        return {"status": "completed"}

    def parse_agent_options(self, config):
        return {"engine": {"key": "e", "model": "m"}}


def _config(tmp_path):
    """Config whose spec lives in a real tmp project scope.

    ``state_dir_for_config`` resolves the scope for real (``.git`` +
    ``.scitex/agent-container/`` markers): state lands under
    ``tmp_path``, never the developer checkout, with no patches.
    """
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / ".scitex" / "agent-container").mkdir(parents=True, exist_ok=True)
    config = AgentConfig(name="worker", harness="opencode", runtime="tui")
    config.config_path = str(tmp_path / "agent" / "spec.yaml")
    return config


def _state_dir(tmp_path):
    return tmp_path / ".scitex" / "agent-container" / "runtime" / "worker"


def test_materialize_workspace_returns_the_first_target(tmp_path):
    # Arrange
    seen = {}

    def materialize(config, state_dir):
        seen["state_dir"] = state_dir
        return [tmp_path / "home", tmp_path / "upper"]

    runtime = OpencodeTuiSessionRuntime(
        gateway=_Gateway(), materialize_fn=materialize
    )
    # Act
    home = runtime.materialize_workspace(_config(tmp_path))
    # Assert
    assert home == tmp_path / "home"


def test_materialize_workspace_targets_the_resolved_state_dir(
    tmp_path, env_save_restore
):
    # Arrange
    seen = {}

    def materialize(config, state_dir):
        seen["state_dir"] = state_dir
        return [tmp_path / "home"]

    runtime = OpencodeTuiSessionRuntime(
        gateway=_Gateway(), materialize_fn=materialize
    )
    # Act
    runtime.materialize_workspace(_config(tmp_path))
    # Assert
    assert seen["state_dir"] == _state_dir(tmp_path)


def test_boot_drain_succeeds_when_the_gateway_answers(tmp_path):
    # Arrange
    state_dir = _state_dir(tmp_path)
    state_dir.mkdir(parents=True)
    (state_dir / "opencode-serve.json").write_text('{"url": "http://x"}')
    runtime = OpencodeTuiSessionRuntime(gateway=_Gateway(states={}))
    # Act
    ready = runtime._drain_at_boot(
        _config(tmp_path), timeout_s=5.0, poll_s=0
    )
    # Assert
    assert ready is True


def test_boot_drain_fails_when_no_serve_state_exists(tmp_path):
    # Arrange
    runtime = OpencodeTuiSessionRuntime(gateway=_Gateway(states={}))
    # Act
    ready = runtime._drain_at_boot(
        _config(tmp_path), timeout_s=0, poll_s=0
    )
    # Assert
    assert ready is False


def test_send_turn_submits_through_the_gateway(tmp_path):
    # Arrange
    gateway = _Gateway()
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    delivered = runtime.send_turn(_config(tmp_path), "hello")
    # Assert
    assert delivered is True


def test_send_turn_carries_the_parsed_engine_options(tmp_path):
    # Arrange
    gateway = _Gateway()
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    runtime.send_turn(_config(tmp_path), "hello")
    # Assert
    assert gateway.submits == [
        ("worker", "hello", "steer", {"engine": {"key": "e", "model": "m"}})
    ]


def test_send_interactive_turn_honours_queue_mode(tmp_path):
    # Arrange
    gateway = _Gateway()
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    runtime.send_interactive_turn(
        _config(tmp_path), "later", delivery_mode="queue"
    )
    # Assert
    assert gateway.submits[0][2] == "queue"


def test_why_not_deliverable_is_none_when_healthy(tmp_path):
    # Arrange
    runtime = OpencodeTuiSessionRuntime(gateway=_Gateway(states={}))
    # Act
    reason = runtime.why_not_deliverable(_config(tmp_path))
    # Assert
    assert reason is None


def test_why_not_deliverable_names_the_gateway_failure(tmp_path):
    # Arrange
    gateway = _Gateway(error=GatewayHarnessError("serve is down"))
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    reason = runtime.why_not_deliverable(_config(tmp_path))
    # Assert
    assert reason == "serve is down"


def test_control_state_reports_a_ready_gateway(tmp_path):
    # Arrange
    gateway = _Gateway(states={"ses_1": "busy"})
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    state = runtime.control_state(_config(tmp_path))
    # Assert
    assert state["gateway_readiness"]["status"] == "ready"


def test_control_state_reports_an_unreachable_gateway(tmp_path):
    # Arrange
    gateway = _Gateway(error=GatewayHarnessError("serve is down"))
    runtime = OpencodeTuiSessionRuntime(gateway=gateway)
    # Act
    state = runtime.control_state(_config(tmp_path))
    # Assert
    assert state["gateway_readiness"]["status"] == "unavailable"
