"""A resumed Hermes turn can work throughout the entire boot window."""
import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTurnActivity, HermesTuiRpcError
from scitex_agent_container.runtimes._tui_boot_drain import wait_for_hermes_boot


@pytest.mark.parametrize("status,state", [("idle", "idle"), ("working", "active"), ("waiting", "active")])
def test_initialized_native_session_is_ready_during_a_turn(status, state):
    config = AgentConfig(name="apps", harness="hermes")
    result = wait_for_hermes_boot(config, exists_fn=lambda: True, timeout_s=1,
        probe_fn=lambda *args, **kwargs: HermesTurnActivity(state, status, "live-session"))
    assert result is True


@pytest.mark.parametrize("observation", ["starting", "unavailable", "empty"])
def test_uninitialized_or_unobservable_session_never_passes_boot(observation):
    clock = [0.0]
    def probe(*args, **kwargs):
        if observation == "unavailable":
            raise HermesTuiRpcError("gateway did not authenticate")
        return HermesTurnActivity("active", "starting" if observation == "starting" else "working",
                                 "" if observation == "empty" else "session")
    result = wait_for_hermes_boot(AgentConfig(name="apps", harness="hermes"),
        exists_fn=lambda: True, timeout_s=1, poll_s=.25, probe_fn=probe,
        time_fn=lambda: clock[0], sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert result is False


def test_session_death_during_native_readiness_is_not_success():
    alive = iter([True, False])
    result = wait_for_hermes_boot(AgentConfig(name="apps", harness="hermes"),
        exists_fn=lambda: next(alive), timeout_s=1,
        probe_fn=lambda *args, **kwargs: HermesTurnActivity("active", "working", "session"))
    assert result is False


class BootMux:
    def __init__(self, pane):
        self.pane = pane
        self.keys = []

    def exists(self, name):
        return True

    def capture_content(self, name):
        return self.pane

    def send_keys(self, name, key):
        self.keys.append(key)


@pytest.mark.parametrize("pane", [
    "The agent is making tool calls; no composer is rendered.",
    "─ brainstorming… │ muse spark\n❯ Ctrl+C to interrupt…\n",
    "─ ready │ muse spark\n❯ a human's staged draft\n",
])
def test_actual_hermes_adapter_accepts_busy_session_without_footer(monkeypatch, pane):
    from scitex_agent_container.runtimes import _hermes_tui_rpc as rpc
    from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime

    mux = BootMux(pane)
    monkeypatch.setattr(rpc, "observe_turn_activity", lambda *args, **kwargs:
        HermesTurnActivity("active", "working", "resumed-session"))
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    assert runtime._drain_at_boot(AgentConfig(name="apps", harness="hermes"),
                                  timeout_s=1, poll_s=0) is True
    assert mux.keys == []


def test_actual_hermes_adapter_rejects_footer_without_native_session(monkeypatch):
    from scitex_agent_container.runtimes import _hermes_tui_rpc as rpc
    from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime

    def unavailable(*args, **kwargs):
        raise HermesTuiRpcError("authenticated session is unavailable")

    monkeypatch.setattr(rpc, "observe_turn_activity", unavailable)
    runtime = HermesTuiSessionRuntime(multiplexer=BootMux("─ ready │ model\n❯\n"))
    assert runtime._drain_at_boot(AgentConfig(name="apps", harness="hermes"),
                                  timeout_s=.03, poll_s=.005) is False


def test_actual_hermes_adapter_refuses_setup_wall_before_rpc(monkeypatch):
    from scitex_agent_container.runtimes import _hermes_tui_rpc as rpc
    from scitex_agent_container.runtimes.hermes_tui import HermesTuiSessionRuntime

    def unexpected_probe(*args, **kwargs):
        raise AssertionError("setup wall must refuse immediately")

    monkeypatch.setattr(rpc, "observe_turn_activity", unexpected_probe)
    runtime = HermesTuiSessionRuntime(multiplexer=BootMux("─ Setup Required │ model\n❯\n"))
    assert runtime._drain_at_boot(AgentConfig(name="apps", harness="hermes"),
                                  timeout_s=1, poll_s=0) is False
