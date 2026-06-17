"""Tests for the TUI A2A turn bridge (``runtimes._tui_turn_bridge``).

The bridge gives a ``runtime: tui`` agent the same ``/v1/turn`` endpoint
the SDK runner serves, so a bus-pushed message (the ``sac mcp channel``
subscriber's wake POST) DRIVES a turn in the idle TUI instead of timing
out on a dead port.

Real seams only (PA-306 no-mocks): the HTTP routes run against a REAL
``ThreadingHTTPServer`` on an ephemeral port, exercised by a REAL urllib
client; a recording ``on_turn`` callable stands in for the tmux inject.
The launcher is driven through a real ``spawn`` recorder (a plain
function), not a mock.

STX-TQ002 AAA markers (each on its own line) + STX-TQ007 one observable
assert + STX-TQ003 descriptive names.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

from scitex_agent_container.runtimes import _tui_turn_bridge as bridge

# A realistic resolved a2a port + a fake PID for the bridge tests
# (PEP 515 separators satisfy STX-NL001).
_PORT = 19_007
_PID = 4_242


# ---------------------------------------------------------------------------
# is_turn_route
# ---------------------------------------------------------------------------
def test_is_turn_route_accepts_bare_v1_turn() -> None:
    # Arrange
    path = "/v1/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_accepts_named_turn_for_this_agent() -> None:
    # Arrange
    path = "/agents/figrecipe/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_rejects_named_route_for_other_agent() -> None:
    # Arrange
    path = "/agents/someone-else/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is False


# ---------------------------------------------------------------------------
# resolved_a2a_port
# ---------------------------------------------------------------------------
def test_resolved_a2a_port_returns_int_when_resolved() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port=_PORT))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port == _PORT


def test_resolved_a2a_port_none_when_port_is_auto_string() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port="auto"))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port is None


# ---------------------------------------------------------------------------
# HTTP server — real ThreadingHTTPServer on an ephemeral port
# ---------------------------------------------------------------------------
@pytest.fixture
def bridge_factory() -> Iterator[Callable[..., int]]:
    """Start real bridge servers on ephemeral ports; tear them all down."""
    servers = []
    threads = []

    def start(on_turn: Callable[[str], None], agent_name: str = "figrecipe") -> int:
        server = bridge.build_server(
            host="127.0.0.1", port=0, on_turn=on_turn, agent_name=agent_name
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        return int(port)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=5)


def _post(port: int, path: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_post_v1_turn_delivers_text_to_on_turn(bridge_factory) -> None:
    # Arrange
    received: list[str] = []
    port = bridge_factory(received.append)
    # Act
    _post(port, "/v1/turn", {"text": "hello fleet"})
    # Assert
    assert received == ["hello fleet"]


def test_post_v1_turn_returns_200_delivered_true(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text: None)
    # Act
    status, body = _post(port, "/v1/turn", {"text": "hi there"})
    # Assert
    assert status == 200 and body.get("delivered") is True


def test_post_named_turn_route_delivers_for_this_agent(bridge_factory) -> None:
    # Arrange
    received: list[str] = []
    port = bridge_factory(received.append, agent_name="figrecipe")
    # Act
    _post(port, "/agents/figrecipe/turn", {"text": "named route"})
    # Assert
    assert received == ["named route"]


def test_post_missing_text_field_returns_400(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text: None)
    # Act
    status, _body = _post(port, "/v1/turn", {"no_text_here": "x"})
    # Assert
    assert status == 400


def test_post_inject_failure_returns_502(bridge_factory) -> None:
    # Arrange
    def raise_session_gone(text: str) -> None:
        raise RuntimeError("session gone")

    port = bridge_factory(raise_session_gone)
    # Act
    status, _body = _post(port, "/v1/turn", {"text": "wake up"})
    # Assert
    assert status == 502


def test_post_unknown_route_returns_404(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text: None)
    # Act
    status, _body = _post(port, "/not/a/turn", {"text": "hi"})
    # Assert
    assert status == 404


def test_health_get_returns_200(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text: None)
    # Act
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
        status = resp.status
    # Assert
    assert status == 200


# ---------------------------------------------------------------------------
# Launcher — real spawn recorder, real PID file
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect HOME so the bridge's state-dir writes stay in the sandbox."""
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_start_turn_bridge_noop_without_resolved_port(tmp_path: Path) -> None:
    # Arrange — an unresolved 'auto' port; spawn must never be invoked.
    def must_not_spawn(*_a, **_k):
        raise AssertionError("spawn must not run without a resolved a2a port")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port="auto"),
        name="figrecipe",
        config_path=str(tmp_path / "spec.yaml"),
    )
    # Act
    pid = bridge.start_turn_bridge(config, spawn=must_not_spawn)
    # Assert
    assert pid is None


def test_start_turn_bridge_passes_resolved_port_to_spawn(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a resolved port + config_path; record the spawn argv.
    spec = tmp_path / "spec.yaml"
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    recorded: dict = {}

    def fake_spawn(argv, **kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn)
    # Assert
    assert "19007" in recorded["argv"]


def test_start_turn_bridge_returns_spawned_pid(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(
        config, spawn=lambda argv, **kw: SimpleNamespace(pid=_PID)
    )
    # Assert
    assert pid == _PID


def test_stop_turn_bridge_noop_when_no_pid_file(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — no bridge was ever started for this agent.
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="never-started", config_path=""
    )
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is False


# ---------------------------------------------------------------------------
# _build_on_turn — the inject callback (runtime DI seam, no tmux)
# ---------------------------------------------------------------------------
def test_build_on_turn_passes_text_and_wait_ready_false() -> None:
    # Arrange — a recording runtime whose send_turn reports delivered.
    seen: list = []

    def fake_send_turn(config, text, wait_ready):
        seen.append((text, wait_ready))
        return True

    runtime = SimpleNamespace(send_turn=fake_send_turn)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="a"), runtime=runtime)
    # Act
    on_turn("wake up")
    # Assert
    assert seen == [("wake up", False)]


def test_build_on_turn_raises_when_session_absent() -> None:
    # Arrange — send_turn reports the session does not exist (returns False).
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: False)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ghost"), runtime=runtime)
    # Act
    # Assert
    with pytest.raises(RuntimeError):
        on_turn("wake up")


# ---------------------------------------------------------------------------
# Launcher — spawn-failure + real SIGTERM teardown
# ---------------------------------------------------------------------------
def test_start_turn_bridge_returns_none_on_spawn_failure(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — spawn raises; the launcher must swallow it and return None.
    spec = tmp_path / "spec.yaml"
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")

    def raising_spawn(argv, **kwargs):
        raise OSError("exec failed")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="boom", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(config, spawn=raising_spawn)
    # Assert
    assert pid is None


def test_stop_turn_bridge_sigterms_recorded_pid(isolated_home: Path) -> None:
    # Arrange — a REAL short-lived process whose PID is recorded as the bridge.
    proc = subprocess.Popen(["sleep", "30"])
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="kill-me", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is True
    proc.wait(timeout=5)
