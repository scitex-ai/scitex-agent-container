"""Regression: wait_until_input_ready accepts the Claude Code v2.1.150 ready state.

paper-scitex-clew handoff (2026-06-20): the scitex-arm capsule agent idled at a
LIVE input behind the first-launch welcome box — `wait_until_input_ready` waited
on the legacy ``? for shortcuts`` marker, which v2.1.150 no longer prints (its
idle state shows the ``bypass permissions`` status bar instead). The drain timed
out ("drained 0 modals") and the startup prompt was never injected.

Fix: accept ``is_ready()`` (the maintained ready check) too, mirroring
``_drain_at_boot``. Real in-memory MultiplexerProtocol — no mocks (PA-306).
"""

from __future__ import annotations

from dataclasses import dataclass

from scitex_agent_container.runtimes.tui_session import (
    TuiInputNotReadyError,
    TuiSessionRuntime,
)

# Actual idle pane captured from a live agent (ywata-note-win, 2026-06-20):
# welcome box + live input (❯) + "bypass permissions" status bar, and crucially
# NO "? for shortcuts" marker.
_V2_READY_PANE = (
    "╭─── Claude Code v2.1.150 ───╮\n"
    "│      Welcome back Yusuke!  │  What's new\n"
    "╰────────────────────────────╯\n"
    "❯ \n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
)
_BOOTING_PANE = "uv pip install -e .[all,dev]\nResolving dependencies ..."


@dataclass
class _Config:
    name: str


class _PaneMux:
    """Minimal real MultiplexerProtocol surface used by wait_until_input_ready:
    a single always-present session returning a fixed pane. Not a mock — a real
    object implementing exactly the methods the runtime calls."""

    def __init__(self, pane: str) -> None:
        self._pane = pane
        self.sent: list[str] = []

    def exists(self, name: str) -> bool:
        return True

    def capture_content(self, name: str) -> str:
        return self._pane

    def send_keys(self, name: str, key: str) -> None:
        self.sent.append(key)


def test_wait_until_input_ready_accepts_v2_1_150_bypass_permissions_state():
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=_PaneMux(_V2_READY_PANE))
    config = _Config(name="capsule")
    # Act
    ready = runtime.wait_until_input_ready(config, timeout_s=1.0, poll_s=0.0)
    # Assert
    assert ready is True


def test_wait_until_input_ready_still_times_out_when_not_ready():
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=_PaneMux(_BOOTING_PANE))
    config = _Config(name="boot")
    # Act
    caught = False
    try:
        runtime.wait_until_input_ready(config, timeout_s=0.2, poll_s=0.0)
    except TuiInputNotReadyError:
        caught = True
    # Assert
    assert caught is True
