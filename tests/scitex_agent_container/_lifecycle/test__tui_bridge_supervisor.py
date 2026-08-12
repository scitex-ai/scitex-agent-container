"""Tests for the TUI turn-bridge supervisor (the heartbeat-tick re-assertion).

Guards the 2026-08-11 incident: 14 of 15 host-side turn bridges were dead
PIDs, nothing supervised them, and live agents whose ``/v1/turn`` port was
unbound refused every pushed wake while still reporting healthy.

No mocks. Every collaborator is real:
  * ``AgentConfig`` / ``A2ASpec`` are the production dataclasses.
  * The port probe is the production ``port_is_free`` bind probe, run against
    REAL sockets that are really bound / really free.
  * The launcher seam is a hand-rolled recording callable with the real
    ``start_turn_bridge`` shape (the module's documented DI seam).

STX-TQ002 AAA-markers each on its own line + STX-TQ007 one-assert.
"""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._tui_bridge_supervisor import (
    DISABLE_ENV,
    VERDICT_FAILED,
    VERDICT_NO_CONFIG,
    VERDICT_NO_SESSION,
    VERDICT_RESTARTED,
    VERDICT_SERVING,
    bridge_is_serving,
    resolve_bridge_port,
    supervise_bridges,
)
from scitex_agent_container.config._types import A2ASpec, AgentConfig

# The REAL bind probe the launcher itself uses — injected as the seam so the
# supervisor's "is anything bound?" question is answered by production code.
from scitex_agent_container.runtimes._tui_turn_bridge_port import port_is_free

PINNED_ACTIVITY_TS = 1_750_000_000

# The supervisor looks an agent up under its ``tui-<name>`` session key, the
# same convention the heartbeat writer uses.
LIVE_SNAPSHOT = {"tui-ag1": PINNED_ACTIVITY_TS, "tui-ag2": PINNED_ACTIVITY_TS}
EMPTY_SNAPSHOT: dict = {}


def _cfg(name: str = "ag1", *, port: Any = "auto", host: str = "127.0.0.1"):
    """A real AgentConfig carrying a real A2ASpec."""
    cfg = AgentConfig(name=name)
    cfg.a2a = A2ASpec(host=host, port=port)
    return cfg


def _agent(name: str = "ag1", *, port: Any = "auto"):
    """One record in the shape ``list_tui_agents`` yields."""
    return {"name": name, "state_dir": None, "config": _cfg(name, port=port)}


class RecordingStart:
    """Real callable with ``start_turn_bridge``'s shape (the launcher seam).

    Records every call so a test can assert WHETHER the bridge was respawned
    and WITH WHAT port — the behaviour under test — without a mock framework.
    """

    def __init__(self, *, pid: int | None = 4242, raises: Exception | None = None):
        self.calls: list[tuple[Any, dict]] = []
        self.pid = pid
        self.raises = raises

    def __call__(self, config: Any, **kwargs: Any) -> int | None:
        self.calls.append((config, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.pid


@contextmanager
def _really_bound_port() -> Iterator[int]:
    """Yield a port with a REAL listening socket on it (bridge-is-up case)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _really_free_port() -> int:
    """Return a port nothing is bound to (bridge-is-dead case)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# resolve_bridge_port — where the supervisor believes the bridge lives
# ---------------------------------------------------------------------------


def test_resolve_bridge_port_returns_the_operator_pinned_int() -> None:
    # Arrange — a spec with a concrete port needs no claim lookup.
    cfg = _cfg(port=19016)
    # Act
    port = resolve_bridge_port(cfg, port_lookup_fn=lambda _n: None)
    # Assert
    assert port == 19016


def test_resolve_bridge_port_falls_back_to_the_allocator_claim_for_auto() -> None:
    # Arrange — every fleet TUI spec declares "auto"; only the claim knows.
    cfg = _cfg(name="scitex-live-paper", port="auto")
    # Act
    port = resolve_bridge_port(cfg, port_lookup_fn=lambda _n: 19015)
    # Assert
    assert port == 19015


def test_resolve_bridge_port_looks_the_claim_up_under_the_agent_name() -> None:
    # Arrange — record which name the claim table is asked about.
    cfg = _cfg(name="scitex-live-paper", port="auto")
    asked: list[str] = []

    def _lookup(name: str) -> int | None:
        asked.append(name)
        return 19015

    # Act
    resolve_bridge_port(cfg, port_lookup_fn=_lookup)
    # Assert
    assert asked == ["scitex-live-paper"]


def test_resolve_bridge_port_is_none_when_the_sidecar_is_disabled() -> None:
    # Arrange — port: None means "no inbound HTTP", and must stay that way.
    cfg = _cfg(port=None)
    # Act
    port = resolve_bridge_port(cfg, port_lookup_fn=lambda _n: 19015)
    # Assert
    assert port is None


def test_resolve_bridge_port_never_consults_the_claim_when_disabled() -> None:
    # Arrange — a disabled sidecar must short-circuit BEFORE the lookup.
    cfg = _cfg(port=None)
    asked: list[str] = []
    # Act
    resolve_bridge_port(cfg, port_lookup_fn=lambda n: asked.append(n) or 19015)
    # Assert
    assert asked == []


def test_resolve_bridge_port_is_none_when_no_claim_exists() -> None:
    # Arrange — an agent that never started has no port anyone POSTs to.
    cfg = _cfg(port="auto")
    # Act
    port = resolve_bridge_port(cfg, port_lookup_fn=lambda _n: None)
    # Assert
    assert port is None


# ---------------------------------------------------------------------------
# bridge_is_serving — the real bind probe against real sockets
# ---------------------------------------------------------------------------


def test_bridge_is_serving_is_true_for_a_really_bound_port() -> None:
    # Arrange — a real listening socket stands in for a live bridge.
    serving = None
    # Act
    with _really_bound_port() as port:
        serving = bridge_is_serving("127.0.0.1", port, port_free_fn=port_is_free)
    # Assert
    assert serving is True


def test_bridge_is_serving_is_false_for_a_really_free_port() -> None:
    # Arrange — nothing is bound here: the dead-bridge case.
    port = _really_free_port()
    # Act
    serving = bridge_is_serving("127.0.0.1", port, port_free_fn=port_is_free)
    # Assert
    assert serving is False


# ---------------------------------------------------------------------------
# supervise_bridges — the fix: a live agent with an unbound port is repaired
# ---------------------------------------------------------------------------


def test_supervisor_restarts_the_bridge_when_the_port_is_unbound() -> None:
    # Arrange — live tmux session, nothing bound: the measured fault.
    start = RecordingStart()
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert len(start.calls) == 1


def test_supervisor_verdict_is_restarted_when_the_port_was_unbound() -> None:
    # Arrange
    start = RecordingStart()
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    verdicts = supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert verdicts["ag1"] == VERDICT_RESTARTED


def test_supervisor_pins_the_resolved_port_onto_the_config_it_relaunches() -> None:
    # Arrange — an "auto" spec must reach the launcher as a concrete int, or
    # start_turn_bridge re-reads "auto", returns None and repairs nothing.
    start = RecordingStart()
    free_port = _really_free_port()
    agents = [_agent("ag1", port="auto")]
    # Act
    supervise_bridges(
        agents,
        snapshot=dict(LIVE_SNAPSHOT),
        start_fn=start,
        port_free_fn=port_is_free,
        port_lookup_fn=lambda _n: free_port,
    )
    # Assert
    assert start.calls[0][0].a2a.port == free_port


def test_supervisor_does_not_relaunch_when_the_port_is_already_bound() -> None:
    # Arrange — a healthy bridge must never be disturbed.
    start = RecordingStart()
    with _really_bound_port() as port:
        agents = [_agent("ag1", port=port)]
        # Act
        supervise_bridges(
            agents,
            snapshot=dict(LIVE_SNAPSHOT),
            start_fn=start,
            port_free_fn=port_is_free,
        )
    # Assert
    assert start.calls == []


def test_supervisor_verdict_is_serving_when_the_port_is_bound() -> None:
    # Arrange
    start = RecordingStart()
    with _really_bound_port() as port:
        agents = [_agent("ag1", port=port)]
        # Act
        verdicts = supervise_bridges(
            agents,
            snapshot=dict(LIVE_SNAPSHOT),
            start_fn=start,
            port_free_fn=port_is_free,
        )
    # Assert
    assert verdicts["ag1"] == VERDICT_SERVING


def test_supervisor_never_relaunches_for_an_agent_with_no_tmux_session() -> None:
    # Arrange — a deliberately-stopped agent must NOT get a resurrected bridge.
    start = RecordingStart()
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    supervise_bridges(
        agents, snapshot=dict(EMPTY_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert start.calls == []


def test_supervisor_verdict_is_no_session_for_a_stopped_agent() -> None:
    # Arrange
    start = RecordingStart()
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    verdicts = supervise_bridges(
        agents, snapshot=dict(EMPTY_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert verdicts["ag1"] == VERDICT_NO_SESSION


def test_supervisor_reports_failed_when_the_launcher_raises() -> None:
    # Arrange — e.g. a foreign process grabbed the port between probe and spawn.
    start = RecordingStart(raises=RuntimeError("port busy"))
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    verdicts = supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert verdicts["ag1"] == VERDICT_FAILED


def test_supervisor_reports_failed_when_the_launcher_returns_no_pid() -> None:
    # Arrange — start_turn_bridge swallows a spawn failure and returns None.
    start = RecordingStart(pid=None)
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    verdicts = supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert verdicts["ag1"] == VERDICT_FAILED


def test_one_agents_failure_does_not_cost_the_next_agent_its_supervision() -> None:
    # Arrange — the first agent's launcher raises; the second must still run.
    calls: list[str] = []

    def _start(config: Any, **_kw: Any) -> int:
        calls.append(config.name)
        if config.name == "ag1":
            raise RuntimeError("boom")
        return 4242

    agents = [_agent("ag1", port=_really_free_port()), _agent("ag2", port=_really_free_port())]
    # Act
    supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=_start, port_free_fn=port_is_free
    )
    # Assert
    assert calls == ["ag1", "ag2"]


def test_supervisor_skips_a_record_that_carries_no_config() -> None:
    # Arrange — a lister that could not load the spec yields no config.
    start = RecordingStart()
    agents = [{"name": "ag1", "state_dir": None}]
    # Act
    verdicts = supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert verdicts["ag1"] == VERDICT_NO_CONFIG


@pytest.fixture
def supervision_disabled() -> Iterator[None]:
    """Set the REAL kill-switch env var, and really remove it on teardown."""
    previous = os.environ.get(DISABLE_ENV)
    os.environ[DISABLE_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DISABLE_ENV, None)
        else:
            os.environ[DISABLE_ENV] = previous


def test_supervisor_is_a_no_op_under_the_env_kill_switch(
    supervision_disabled: None,
) -> None:
    # Arrange
    start = RecordingStart()
    agents = [_agent("ag1", port=_really_free_port())]
    # Act
    supervise_bridges(
        agents, snapshot=dict(LIVE_SNAPSHOT), start_fn=start, port_free_fn=port_is_free
    )
    # Assert
    assert start.calls == []
