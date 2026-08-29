"""Tests for _lifecycle.health — probe-style helpers + monitor loop.

No mocks. All collaborators are real:
  * ``AgentConfig`` is the real production dataclass.
  * A2A endpoints are real local HTTP servers in background threads.
  * The ``runtime`` argument to ``_check_sdk_alive`` is a hand-rolled
    fake class with the real ``is_running`` shape (production seam).
  * ``health_monitor`` accepts ``health_check_fn`` and ``sleep_fn``
    seams (real callable defaults) — tests pass real Python callables.
  * The Registry is the real on-disk file-based ``Registry`` in
    ``tmp_path``.
"""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import health as health_mod
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config._types import (
    A2ASpec,
    AgentConfig,
    HealthSpec,
    RestartSpec,
)
from scitex_agent_container.runtimes._tui_turn_bridge import build_server

# ---------------------------------------------------------------------------
# Real fakes & helpers (no unittest.mock)
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Hand-rolled real collaborator implementing the runtime seam.

    Matches the surface ``_check_sdk_alive`` actually uses:
    ``is_running(config) -> bool``. Records calls for assertions.
    """

    def __init__(self, *, running: bool) -> None:
        self.running = running
        self.calls: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        self.calls.append(config)
        return self.running


def _make_cfg(
    name: str = "ag1",
    method: str = "sdk-alive",
    policy: str = "never",
    max_retries: int = 3,
) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.health = HealthSpec(method=method, interval=0)
    cfg.restart = RestartSpec(
        policy=policy,
        max_retries=max_retries,
        backoff_initial=0,
        backoff_max=0,
        backoff_multiplier=2,
    )
    return cfg


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server  # type: ignore[assignment]
        status = getattr(srv, "status_code", 200)
        body = getattr(srv, "body", b"")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def real_http_server() -> Iterator[Any]:
    """Spin up a real HTTPServer on 127.0.0.1 in a background thread."""
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.status_code = 200  # type: ignore[attr-defined]
    server.body = b""  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Controller:
        def __init__(self) -> None:
            self.port = port

        def set_response(self, *, status: int, body: bytes) -> None:
            server.status_code = status  # type: ignore[attr-defined]
            server.body = body  # type: ignore[attr-defined]

    try:
        yield Controller()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_a2a_yaml(
    tmp_path: Path,
    *,
    port: int | None,
    host: str = "127.0.0.1",
    include_a2a: bool = True,
) -> Path:
    """Write a real YAML file matching the shape ``_read_a2a_block`` parses."""
    if include_a2a and port is not None:
        body = f"spec:\n  a2a:\n    host: {host}\n    port: {port}\n"
    else:
        body = "spec: {}\n"
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(body)
    return yaml_path


# ---------------------------------------------------------------------------
# health_check dispatcher
# ---------------------------------------------------------------------------


def test_health_check_unknown_method_returns_false_flag() -> None:
    # Arrange
    cfg = _make_cfg(method="bogus")
    # Act
    ok, _msg = health_mod.health_check(cfg)
    # Assert
    assert ok is False


def test_health_check_unknown_method_message_names_method() -> None:
    # Arrange
    cfg = _make_cfg(method="bogus")
    # Act
    _ok, msg = health_mod.health_check(cfg)
    # Assert
    assert "Unknown health method" in msg


def test_health_check_sdk_alive_routes_to_sdk_helper(pg_schema, ) -> None:
    # Arrange: a real fake runtime that reports healthy.
    cfg = _make_cfg(method="sdk-alive")
    runtime = FakeRuntime(running=True)
    # Act
    result = health_mod.health_check(cfg, runtime=runtime)
    # Assert: routing landed in _check_sdk_alive which consulted runtime.
    assert result == (True, "healthy")


def test_health_check_sdk_alive_passes_config_to_runtime(pg_schema, ) -> None:
    # Arrange
    cfg = _make_cfg(method="sdk-alive")
    runtime = FakeRuntime(running=True)
    # Act
    health_mod.health_check(cfg, runtime=runtime)
    # Assert
    assert runtime.calls == [cfg]


def test_health_check_a2a_card_routes_to_a2a_helper(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange: real YAML + real HTTP server returning a wrong-name card.
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(
        status=200, body=json.dumps({"name": "different"}).encode()
    )
    # Act
    _ok, msg = health_mod.health_check(cfg)
    # Assert: a2a-card branch reached (name mismatch path is a2a-specific).
    assert "name mismatch" in msg


# ---------------------------------------------------------------------------
# _check_sdk_alive (uses injected real runtime collaborator)
# ---------------------------------------------------------------------------


def test_check_sdk_alive_healthy_flag_when_runtime_running() -> None:
    # Arrange
    cfg = _make_cfg()
    runtime = FakeRuntime(running=True)
    # Act
    ok, _msg = health_mod._check_sdk_alive(cfg, runtime=runtime)
    # Assert
    assert ok is True


def test_check_sdk_alive_healthy_message_when_runtime_running() -> None:
    # Arrange
    cfg = _make_cfg()
    runtime = FakeRuntime(running=True)
    # Act
    _ok, msg = health_mod._check_sdk_alive(cfg, runtime=runtime)
    # Assert
    assert msg == "healthy"


def test_check_sdk_alive_unhealthy_flag_when_runtime_stopped() -> None:
    # Arrange
    cfg = _make_cfg()
    runtime = FakeRuntime(running=False)
    # Act
    ok, _msg = health_mod._check_sdk_alive(cfg, runtime=runtime)
    # Assert
    assert ok is False


def test_check_sdk_alive_unhealthy_message_names_the_runtime_kind() -> None:
    """REPLACES an assertion on the literal "SDK runner not running".

    That text was emitted for EVERY runtime, including ``tui`` agents that
    have no SDK runner at all, so it sent readers hunting a process that
    never existed. The message now names the runtime actually consulted.
    """
    # Arrange
    cfg = _make_cfg()
    runtime = FakeRuntime(running=False)
    # Act
    _ok, msg = health_mod._check_sdk_alive(cfg, runtime=runtime)
    # Assert
    assert "tui runtime reports its process not running" in msg


def test_check_sdk_alive_unhealthy_message_names_an_explicit_sdk_runtime() -> None:
    # Arrange
    cfg = _make_cfg()
    cfg.runtime = "claude-agent-sdk"
    runtime = FakeRuntime(running=False)
    # Act
    _ok, msg = health_mod._check_sdk_alive(cfg, runtime=runtime)
    # Assert
    assert "claude-agent-sdk runtime reports its process not running" in msg


def test_default_runtime_resolution_uses_the_canonical_selector() -> None:
    """The regression this PR exists for.

    ``_check_sdk_alive`` used to hardcode ``ClaudeSessionRuntime``, whose
    ``is_running`` returns False for any spec the container-runtime lookup
    cannot resolve — and ``spec.runtime`` DEFAULTS to ``tui``. So the default
    configuration reported unhealthy while alive, and ``sac agents health``
    exits non-zero on that bool. Pinning the resolution to the canonical
    selector is what fixes it, so the test pins the resolution, not a message.
    """
    # Arrange
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = _make_cfg()
    # Act
    resolved = _get_runtime(cfg)
    # Assert
    assert isinstance(resolved, TuiSessionRuntime)


def test_default_runtime_is_tui_so_the_default_path_is_the_tui_path() -> None:
    """Pins WHY the bug was fleet-wide rather than opt-in."""
    # Arrange
    cfg = _make_cfg()
    # Act
    runtime_kind = getattr(cfg, "runtime", "")
    # Assert
    assert runtime_kind == "tui"


def test_explicit_sdk_runtime_still_resolves_to_the_sdk_runtime() -> None:
    """The fix must not change behaviour for specs that really are SDK."""
    # Arrange
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    cfg = _make_cfg()
    cfg.runtime = "claude-agent-sdk"
    # Act
    resolved = _get_runtime(cfg)
    # Assert
    assert isinstance(resolved, ClaudeSessionRuntime)


# ---------------------------------------------------------------------------
# _check_a2a_card — real local HTTP server
# ---------------------------------------------------------------------------


def test_check_a2a_card_missing_block_returns_unhealthy(tmp_path: Path) -> None:
    # Arrange: real YAML with no spec.a2a block.
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=None, include_a2a=False))
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert "spec.a2a not set" in msg


def test_check_a2a_card_happy_returns_healthy_flag(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(status=200, body=json.dumps({"name": "ag1"}).encode())
    # Act
    ok, _msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert ok is True


def test_check_a2a_card_happy_message_reports_endpoint(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(status=200, body=json.dumps({"name": "ag1"}).encode())
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert f"127.0.0.1:{real_http_server.port}" in msg


def test_check_a2a_card_http_error_includes_status_code(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange: real server returns 503.
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(status=503, body=b"boom")
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert "HTTP 503" in msg


def test_check_a2a_card_url_error_when_port_closed(tmp_path: Path, dead_port) -> None:
    # Arrange: YAML points at a port bound but never listened on, and HELD —
    # so it is guaranteed closed for the whole test, not merely closed at the
    # moment we looked. (`_free_port` above is the OTHER need: a port a real
    # server is about to bind. It must not be used for a must-stay-dead port.)
    closed_port = dead_port()
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=closed_port))
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert "unreachable" in msg


def test_check_a2a_card_bad_json_returns_unhealthy(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange: real server returns malformed JSON.
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(status=200, body=b"not json{")
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert "malformed JSON" in msg


def test_check_a2a_card_name_mismatch_returns_unhealthy(
    tmp_path: Path, real_http_server: Any
) -> None:
    # Arrange: real server returns valid JSON with wrong name.
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(
        status=200, body=json.dumps({"name": "different"}).encode()
    )
    # Act
    _ok, msg = health_mod._check_a2a_card(cfg)
    # Assert
    assert "name mismatch" in msg


def test_check_a2a_card_non_dict_payload_raises_attribute_error(
    tmp_path: Path, real_http_server: Any
) -> None:
    """Document an extant production bug: when the AgentCard payload is a
    JSON list (not a dict), ``_check_a2a_card`` constructs its
    mismatch-branch f-string by calling ``data.get('name')`` before the
    ``isinstance(data, dict)`` short-circuit fires, so the f-string
    formatting raises ``AttributeError`` (lists have no ``.get``).

    This test pins the current buggy behaviour. Fix the production
    code to short-circuit on non-dict before formatting and this test
    must be updated to assert ``"name mismatch" in msg``.
    """
    # Arrange: real server returns a JSON list (not a dict).
    cfg = _make_cfg(name="ag1", method="a2a-card")
    cfg.config_path = str(_write_a2a_yaml(tmp_path, port=real_http_server.port))
    real_http_server.set_response(status=200, body=b"[]")
    # Act
    call = lambda: health_mod._check_a2a_card(cfg)  # noqa: E731
    # Assert
    with pytest.raises(AttributeError):
        call()


# ---------------------------------------------------------------------------
# health_monitor loop — real Registry, real sleep_fn, real health_check_fn
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    """Real on-disk Registry rooted in tmp_path."""
    return Registry(registry_dir=tmp_path / "registry")


def _no_sleep(_seconds: float) -> None:
    return None


class ScriptedHealth:
    """Real callable matching the ``health_check_fn`` contract.

    Returns each scripted ``(bool, str)`` in turn; sticks on the last
    after exhaustion. Records calls.
    """

    def __init__(self, results: list[tuple[bool, str]]) -> None:
        self._results = list(results)
        self.calls: list[AgentConfig] = []

    def __call__(self, config: AgentConfig) -> tuple[bool, str]:
        self.calls.append(config)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def test_health_monitor_exits_when_registry_has_no_agent(
    registry: Registry,
) -> None:
    # Arrange: registry empty → exists(name) is False on first poll.
    cfg = _make_cfg(policy="never")
    calls: list[AgentConfig] = []

    def record(c: AgentConfig) -> tuple[bool, str]:
        calls.append(c)
        return (True, "ok")

    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        health_check_fn=record,
        sleep_fn=_no_sleep,
    )
    # Assert: loop exited before invoking the checker.
    assert calls == []


def test_health_monitor_never_policy_does_not_restart(
    registry: Registry, tmp_path: Path
) -> None:
    # Arrange: register agent so first poll proceeds, then de-register so the
    # second poll exits. health_check returns unhealthy throughout.
    cfg = _make_cfg(policy="never")
    registry.add("ag1", config_path=str(tmp_path / "cfg.yaml"), screen_name="s")
    check = ScriptedHealth([(False, "bad")])
    restart_calls: list[AgentConfig] = []

    def check_then_remove(c: AgentConfig) -> tuple[bool, str]:
        registry.remove("ag1")
        return check(c)

    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        restart_fn=lambda c: restart_calls.append(c),
        health_check_fn=check_then_remove,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert restart_calls == []


def test_health_monitor_on_failure_calls_restart_up_to_max_retries(
    registry: Registry, tmp_path: Path
) -> None:
    # Arrange
    cfg = _make_cfg(policy="on-failure", max_retries=2)
    registry.add("ag1", config_path=str(tmp_path / "cfg.yaml"), screen_name="s")
    check = ScriptedHealth([(False, "bad")])
    restart_calls: list[AgentConfig] = []
    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        restart_fn=lambda c: restart_calls.append(c),
        health_check_fn=check,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert len(restart_calls) == 2


def test_health_monitor_resets_retries_after_healthy(
    registry: Registry, tmp_path: Path
) -> None:
    # Arrange: 1 fail → restart → healthy (reset) → 2 fails → restart twice → give up.
    cfg = _make_cfg(policy="always", max_retries=2)
    registry.add("ag1", config_path=str(tmp_path / "cfg.yaml"), screen_name="s")
    check = ScriptedHealth(
        [
            (False, "x"),
            (True, "ok"),
            (False, "x"),
            (False, "x"),
            (False, "x"),
        ]
    )
    restart_calls: list[AgentConfig] = []
    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        restart_fn=lambda c: restart_calls.append(c),
        health_check_fn=check,
        sleep_fn=_no_sleep,
    )
    # Assert: 1 (pre-reset) + 2 (post-reset) = 3 restarts.
    assert len(restart_calls) == 3


def test_health_monitor_swallows_restart_fn_exception(
    registry: Registry, tmp_path: Path
) -> None:
    # Arrange
    cfg = _make_cfg(policy="on-failure", max_retries=1)
    registry.add("ag1", config_path=str(tmp_path / "cfg.yaml"), screen_name="s")

    def bad_restart(_c: AgentConfig) -> None:
        raise RuntimeError("kaboom")

    check = ScriptedHealth([(False, "bad")])
    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        restart_fn=bad_restart,
        health_check_fn=check,
        sleep_fn=_no_sleep,
    )
    # Assert: monitor reached the unhealthy branch and tried the restart_fn
    # at least once; the raised exception did not abort the loop (we got
    # here without propagation).
    assert len(check.calls) >= 1


def test_health_monitor_no_restart_fn_with_unhealthy_returns_cleanly(
    registry: Registry, tmp_path: Path
) -> None:
    # Arrange: restart_fn is None, policy on-failure, max_retries reached
    # → loop must still return cleanly (does not raise).
    cfg = _make_cfg(policy="on-failure", max_retries=1)
    registry.add("ag1", config_path=str(tmp_path / "cfg.yaml"), screen_name="s")
    check = ScriptedHealth([(False, "bad")])
    # Act
    health_mod.health_monitor(
        "ag1",
        cfg,
        registry,
        restart_fn=None,
        health_check_fn=check,
        sleep_fn=_no_sleep,
    )
    # Assert: loop polled at least once with no restart callable and exited.
    assert len(check.calls) >= 1


# ---------------------------------------------------------------------------
# Turn-bridge health gate (2026-08-11 incident)
# ---------------------------------------------------------------------------
# 14 of 15 host-side TUI turn bridges were dead PIDs and every one of those
# agents still reported GREEN, because `sdk-alive` only asks whether the tmux
# session is alive. These tests pin the gate that makes a dead bridge fail.
#
# The server under test is the PRODUCTION `_TurnBridgeServer` (via the real
# `build_server`), so the `/health` route being probed is the actual route the
# real bridge serves — not a stand-in that could drift away from it.


@contextmanager
def _real_turn_bridge(*, port: int, agent_name: str) -> Iterator[Any]:
    """Run a REAL turn bridge on ``port`` in a background thread."""
    server = build_server(
        host="127.0.0.1",
        port=port,
        on_turn=lambda *_a, **_kw: None,
        agent_name=agent_name,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _bridge_cfg(
    name: str = "ag1",
    *,
    port: Any,
    runtime: str = "tui",
    method: str = "sdk-alive",
) -> AgentConfig:
    """A real config whose a2a port is pinned (no claim lookup needed)."""
    cfg = _make_cfg(name=name, method=method)
    cfg.runtime = runtime
    cfg.a2a = A2ASpec(host="127.0.0.1", port=port)
    return cfg


def _no_delay(_seconds: float) -> None:
    """Real sleep seam that does not actually sleep."""
    return None


def test_sdk_alive_fails_a_live_tui_agent_whose_turn_bridge_is_dead(dead_port) -> None:
    # Arrange — the measured fault: tmux session alive, nothing bound to the
    # a2a port. This is the case that used to report healthy. The port is HELD
    # unbound, so no other test can bind it and make the bridge look alive.
    cfg = _bridge_cfg(port=dead_port())
    # Act
    healthy, _message = health_mod.health_check(cfg, runtime=FakeRuntime(running=True))
    # Assert
    assert healthy is False


def test_dead_turn_bridge_message_names_the_unserved_url(dead_port) -> None:
    # Arrange
    port = dead_port()
    cfg = _bridge_cfg(port=port)
    # Act
    _healthy, message = health_mod.health_check(cfg, runtime=FakeRuntime(running=True))
    # Assert
    assert f"http://127.0.0.1:{port}/health" in message


def test_sdk_alive_passes_a_tui_agent_whose_turn_bridge_is_serving() -> None:
    # Arrange — a REAL bridge on the agent's port.
    port = _free_port()
    cfg = _bridge_cfg(port=port)
    # Act
    with _real_turn_bridge(port=port, agent_name="ag1"):
        healthy, _message = health_mod.health_check(
            cfg, runtime=FakeRuntime(running=True)
        )
    # Assert
    assert healthy is True


def test_healthy_message_reports_the_turn_bridge_alongside_the_runtime() -> None:
    # Arrange
    port = _free_port()
    cfg = _bridge_cfg(port=port)
    # Act
    with _real_turn_bridge(port=port, agent_name="ag1"):
        _healthy, message = health_mod.health_check(
            cfg, runtime=FakeRuntime(running=True)
        )
    # Assert
    assert "turn bridge healthy" in message


def test_a_dead_runtime_dominates_the_bridge_verdict(dead_port) -> None:
    # Arrange — a stopped agent must be reported as a stopped RUNTIME, not as
    # a missing bridge (naming the symptom would send the reader hunting).
    cfg = _bridge_cfg(port=dead_port())
    # Act
    _healthy, message = health_mod.health_check(cfg, runtime=FakeRuntime(running=False))
    # Assert
    assert "runtime reports its process not running" in message


def test_the_bridge_gate_is_skipped_when_the_a2a_sidecar_is_disabled() -> None:
    # Arrange — port None means "no inbound HTTP": there is no bridge to miss.
    cfg = _bridge_cfg(port=None)
    # Act
    healthy, _message = health_mod.health_check(cfg, runtime=FakeRuntime(running=True))
    # Assert
    assert healthy is True


def test_the_bridge_gate_is_skipped_for_a_non_tui_runtime() -> None:
    # Arrange — an SDK agent serves /v1/turn from its own runner, not a bridge.
    cfg = _bridge_cfg(port=_free_port(), runtime="claude-agent-sdk")
    # Act
    healthy, _message = health_mod.health_check(cfg, runtime=FakeRuntime(running=True))
    # Assert
    assert healthy is True


def test_turn_bridge_method_probes_the_bridge_directly() -> None:
    # Arrange — the opt-in method, with a real bridge answering.
    port = _free_port()
    cfg = _bridge_cfg(port=port, method="turn-bridge")
    # Act
    with _real_turn_bridge(port=port, agent_name="ag1"):
        healthy, _message = health_mod.health_check(cfg)
    # Assert
    assert healthy is True


def test_turn_bridge_method_fails_when_nothing_is_bound(dead_port) -> None:
    # Arrange
    cfg = _bridge_cfg(port=dead_port(), method="turn-bridge")
    # Act
    healthy, _message = health_mod.health_check(cfg)
    # Assert
    assert healthy is False


# --- corroboration: one observation is not a verdict (issue #825's family) ---


def test_a_bridge_that_binds_during_the_backoff_is_reported_healthy() -> None:
    # Arrange — the respawn window our OWN supervisor creates: the first probe
    # is genuinely refused, then a REAL bridge binds before the second.
    port = _free_port()
    cfg = _bridge_cfg(port=port)
    running: dict = {}

    def _bind_during_backoff(_seconds: float) -> None:
        if "server" in running:
            return
        server = build_server(
            host="127.0.0.1",
            port=port,
            on_turn=lambda *_a, **_kw: None,
            agent_name="ag1",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running["server"] = server
        running["thread"] = thread

    # Act
    try:
        healthy, _message = health_mod._check_turn_bridge(
            cfg, sleep_fn=_bind_during_backoff
        )
    finally:
        if "server" in running:
            running["server"].shutdown()
            running["server"].server_close()
            running["thread"].join(timeout=2)
    # Assert
    assert healthy is True


def test_a_persistently_dead_bridge_is_failed_after_every_attempt(dead_port) -> None:
    # Arrange — PERSISTENTLY dead: the port is held unbound across all three
    # probes, so "still dead on attempt 3" is a property, not a coincidence.
    cfg = _bridge_cfg(port=dead_port())
    # Act
    healthy, _message = health_mod._check_turn_bridge(cfg, sleep_fn=_no_delay)
    # Assert
    assert healthy is False


def test_a_failed_verdict_states_how_many_probes_confirmed_it(dead_port) -> None:
    # Arrange
    cfg = _bridge_cfg(port=dead_port())
    # Act
    _healthy, message = health_mod._check_turn_bridge(
        cfg, attempts=3, sleep_fn=_no_delay
    )
    # Assert
    assert "confirmed by 3 probes" in message


def test_a_foreign_holder_is_failed_without_retrying() -> None:
    # Arrange — a REAL bridge for a DIFFERENT agent holds this agent's port.
    # That is settled, not transient, so it must not cost three probes.
    port = _free_port()
    cfg = _bridge_cfg(name="ag1", port=port)
    naps: list[float] = []
    # Act
    with _real_turn_bridge(port=port, agent_name="someone-else"):
        health_mod._check_turn_bridge(cfg, sleep_fn=lambda s: naps.append(s))
    # Assert
    assert naps == []


def test_a_foreign_holder_message_names_the_agent_that_answered() -> None:
    # Arrange
    port = _free_port()
    cfg = _bridge_cfg(name="ag1", port=port)
    # Act
    with _real_turn_bridge(port=port, agent_name="someone-else"):
        _healthy, message = health_mod._check_turn_bridge(cfg, sleep_fn=_no_delay)
    # Assert
    assert "someone-else" in message
