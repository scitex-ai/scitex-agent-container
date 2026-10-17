"""Hermes boot-drain readiness: a resumed turn counts throughout boot.

A native working session proves boot readiness without waiting for idle;
an unobservable session, a dead session, a setup wall, or a missing
native session refuses. The pane contract: a busy adapter without a
footer still drains (no interrupt hint is ever sent to a human's staged
input).

PA-306 no-mocks: the native probe is substituted through a fake
``__import__`` boundary (the ``import_boundary`` pattern from
test_hermes_tui.py) — the rebound ``_drain_at_boot`` code object keeps
production imports intact while ``_tui_boot_drain`` resolves to a gate
injecting the fake activity producer. No process mocking.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._hermes_tui_rpc import (
    HermesTuiRpcError,
    HermesTurnActivity,
)
from scitex_agent_container.runtimes._tui_boot_drain import wait_for_hermes_boot


def _working(session: str = "live-session"):
    """Fake native probe reporting an initialized working session."""

    def probe(*args, **kwargs):
        return HermesTurnActivity("active", "working", session)

    return probe


@pytest.mark.parametrize(
    "status,state", [("idle", "idle"), ("working", "active"), ("waiting", "active")]
)
def test_initialized_native_session_is_ready_during_a_turn(status, state):
    # Arrange — a native session already past setup.
    activity = HermesTurnActivity("active", status, "live-session")
    # Act
    result = wait_for_hermes_boot(
        AgentConfig(name="apps", harness="hermes"),
        exists_fn=lambda: True,
        timeout_s=1,
        probe_fn=lambda *args, **kwargs: activity,
    )
    # Assert
    assert result is True


@pytest.mark.parametrize("observation", ["starting", "unavailable", "empty"])
def test_uninitialized_or_unobservable_session_never_passes_boot(observation):
    # Arrange — fake clock so the refusal costs no wall time.
    clock = [0.0]

    def probe(*args, **kwargs):
        if observation == "unavailable":
            raise HermesTuiRpcError("gateway did not authenticate")
        return HermesTurnActivity(
            "active",
            "starting" if observation == "starting" else "working",
            "" if observation == "empty" else "session",
        )

    # Act
    result = wait_for_hermes_boot(
        AgentConfig(name="apps", harness="hermes"),
        exists_fn=lambda: True,
        timeout_s=1,
        poll_s=0.25,
        probe_fn=probe,
        time_fn=lambda: clock[0],
        sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    # Assert
    assert result is False


def test_session_death_during_native_readiness_is_not_success():
    # Arrange — the session vanishes mid-probe.
    alive = iter([True, False])
    # Act
    result = wait_for_hermes_boot(
        AgentConfig(name="apps", harness="hermes"),
        exists_fn=lambda: next(alive),
        timeout_s=1,
        probe_fn=_working(),
    )
    # Assert
    assert result is False


def _rebound_drain(mux, probe):
    """Rebind ``_drain_at_boot`` with a fake ``_tui_boot_drain`` gate.

    The ``import_boundary`` pattern from test_hermes_tui.py: production
    imports stay intact while the native gate resolves to a wrapper
    injecting ``probe`` as the activity producer.
    """
    import builtins
    from types import FunctionType, SimpleNamespace

    from scitex_agent_container.runtimes import hermes_tui

    def native_gate(config, **kwargs):
        return wait_for_hermes_boot(config, probe_fn=probe, **kwargs)

    def import_boundary(name, *args, **kwargs):
        if name == "_tui_boot_drain":
            return SimpleNamespace(wait_for_hermes_boot=native_gate)
        return builtins.__import__(name, *args, **kwargs)

    runtime = hermes_tui.HermesTuiSessionRuntime.__new__(
        hermes_tui.HermesTuiSessionRuntime
    )
    runtime._mux = mux
    namespace = {
        **vars(hermes_tui),
        "__builtins__": {**vars(builtins), "__import__": import_boundary},
    }
    return runtime, FunctionType(runtime._drain_at_boot.__func__.__code__, namespace)


class _BootMux:
    """Minimal multiplexer double: fixed pane, records sent keys."""

    def __init__(self, pane):
        self.pane = pane
        self.keys = []

    def exists(self, name):
        return True

    def capture_content(self, name):
        return self.pane

    def send_keys(self, name, key):
        self.keys.append(key)


class _Cfg:
    name = "apps"


@pytest.mark.parametrize(
    "pane",
    [
        "The agent is making tool calls; no composer is rendered.",
        "─ brainstorming… │ muse spark\n❯ Ctrl+C to interrupt…\n",
        "─ ready │ muse spark\n❯ a human's staged draft\n",
    ],
)
def test_busy_session_without_footer_drains_as_ready(pane):
    # Arrange — busy adapter, no composer/footer, live native session.
    runtime, drain = _rebound_drain(_BootMux(pane), _working("resumed-session"))
    # Act
    result = drain(runtime, _Cfg(), timeout_s=1, poll_s=0)
    # Assert
    assert result is True


@pytest.mark.parametrize(
    "pane",
    [
        "The agent is making tool calls; no composer is rendered.",
        "─ brainstorming… │ muse spark\n❯ Ctrl+C to interrupt…\n",
        "─ ready │ muse spark\n❯ a human's staged draft\n",
    ],
)
def test_busy_session_drain_sends_no_interrupt_hint(pane):
    # Arrange — same busy panes; the drain must not type into them.
    mux = _BootMux(pane)
    runtime, drain = _rebound_drain(mux, _working("resumed-session"))
    # Act
    drain(runtime, _Cfg(), timeout_s=1, poll_s=0)
    # Assert — no Ctrl+C hint was submitted to a human's staged input.
    assert mux.keys == []


def test_drain_refuses_footer_without_native_session():
    # Arrange — ready-looking footer but the native session is gone.
    def unavailable(*args, **kwargs):
        raise HermesTuiRpcError("authenticated session is unavailable")

    runtime, drain = _rebound_drain(_BootMux("─ ready │ model\n❯\n"), unavailable)
    # Act
    result = drain(runtime, _Cfg(), timeout_s=0.03, poll_s=0.005)
    # Assert
    assert result is False


def test_drain_refuses_setup_wall_before_any_probe():
    # Arrange — setup wall: refusal must precede any native probe.
    def unexpected_probe(*args, **kwargs):
        raise AssertionError("setup wall must refuse immediately")

    runtime, drain = _rebound_drain(
        _BootMux("─ Setup Required │ model\n❯\n"), unexpected_probe
    )
    # Act
    result = drain(runtime, _Cfg(), timeout_s=1, poll_s=0)
    # Assert
    assert result is False
