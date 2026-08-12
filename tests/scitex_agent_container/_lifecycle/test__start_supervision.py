"""The post-start daemon threads, extracted from ``_start`` under the 512-cap.

Covers ``_lifecycle/_start_supervision.start_background_supervision``. The
behaviour worth pinning is the rule both supervisors share: they watch an agent
that is ALREADY up, so neither may take the start down with it — a supervisor
that can fail the start it was added to protect is an outage generator.

Real recording collaborators through the documented ``thread_factory`` /
``handover`` seams — no mocks (PA-306). STX-TQ002 AAA markers, STX-TQ007 one
assert per test.

Named ``test__start_supervision.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/_lifecycle/_start_supervision.py``.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._start_supervision import (
    start_background_supervision,
)
from scitex_agent_container.config._types import AgentConfig


class _RecordingThread:
    """A real thread-shaped object that records ``start()`` instead of running."""

    started: list[str] = []

    def __init__(self, *, target=None, args=(), daemon=False) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self) -> None:
        _RecordingThread.started.append(getattr(self.target, "__name__", "?"))


class _Handover:
    """A real handover module stand-in that records the failback call."""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[str] = []
        self.explode = explode

    def start_failback_poller(self, config) -> None:
        if self.explode:
            raise RuntimeError("hub unreachable")
        self.calls.append(config.name)


def _cfg(name: str, *, health: bool) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.health.enabled = health
    return cfg


def test_health_monitor_thread_is_started_when_health_is_enabled() -> None:
    # Arrange
    _RecordingThread.started = []
    handover = _Handover()
    # Act
    start_background_supervision(
        _cfg("zz-sup-on", health=True),
        registry=None,
        runtime_factory=lambda cfg: None,
        handover=handover,
        thread_factory=_RecordingThread,
    )
    # Assert
    assert _RecordingThread.started == ["health_monitor"]


def test_no_health_thread_when_health_is_disabled() -> None:
    # Arrange
    _RecordingThread.started = []
    handover = _Handover()
    # Act
    start_background_supervision(
        _cfg("zz-sup-off", health=False),
        registry=None,
        runtime_factory=lambda cfg: None,
        handover=handover,
        thread_factory=_RecordingThread,
    )
    # Assert
    assert _RecordingThread.started == []


def test_the_failback_poller_is_launched() -> None:
    # Arrange
    _RecordingThread.started = []
    handover = _Handover()
    # Act
    start_background_supervision(
        _cfg("zz-sup-poll", health=False),
        registry=None,
        runtime_factory=lambda cfg: None,
        handover=handover,
        thread_factory=_RecordingThread,
    )
    # Assert
    assert handover.calls == ["zz-sup-poll"]


def test_a_failing_failback_poller_does_not_fail_the_start() -> None:
    # Arrange — the agent is already up; an optional multi-host optimisation
    # failing to launch must not unwind a start that already succeeded.
    _RecordingThread.started = []
    handover = _Handover(explode=True)
    returned = "not-called"
    # Act
    returned = start_background_supervision(
        _cfg("zz-sup-boom", health=False),
        registry=None,
        runtime_factory=lambda cfg: None,
        handover=handover,
        thread_factory=_RecordingThread,
    )
    # Assert
    assert returned is None
