"""Coverage for ``_listen._forward.forward_to_live_runner`` (PA-306 no-mocks).

Spins up a real loopback HTTP server with :func:`asyncio.start_server`
that speaks just enough of HTTP/1.1 to satisfy ``urllib.request`` and
drives the production ``forward_to_live_runner`` against it. No mocks,
no monkeypatching of the transport — only environment + module
constants are repointed at ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

# --- Test doubles for the *config* arg (not the I/O path) -------------------


@dataclass
class _A2A:
    host: str | None = None
    port: object = None


@dataclass
class _Cfg:
    a2a: _A2A | None = None


# --- Real loopback HTTP server fixture --------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LoopbackHTTP:
    """Bare-bones HTTP/1.1 responder backed by ``asyncio.start_server``.

    Reads the request line + headers + Content-Length body, then replies
    with the canned ``status``/``body`` configured per test. Captures the
    last request body so assertions can inspect what the production code
    sent.
    """

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
        self.last_body: bytes | None = None
        self.last_path: str | None = None
        self._server: asyncio.base_events.Server | None = None
        self.port: int = _free_port()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_line = await reader.readline()
        try:
            self.last_path = request_line.decode().split(" ", 2)[1]
        except Exception:
            self.last_path = None
        content_length = 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        self.last_body = (
            await reader.readexactly(content_length) if content_length else b""
        )

        reason = {200: "OK", 404: "Not Found", 500: "Server Error"}.get(
            self.status, "X"
        )
        response = (
            f"HTTP/1.1 {self.status} {reason}\r\n"
            f"Content-Length: {len(self.body)}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + self.body
        writer.write(response)
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def __aenter__(self) -> "_LoopbackHTTP":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


# --- Common fixture: isolate port_allocator's state.db ----------------------


@pytest.fixture
def isolated_state_db(tmp_path: Path):
    """Point ``state_db.DEFAULT_DB_PATH`` (and the env) at ``tmp_path``."""
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(tmp_path / "state.db")
    import scitex_agent_container._state.state_db as state_db_mod

    importlib.reload(state_db_mod)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(state_db_mod)


# --- Tests ------------------------------------------------------------------


def test_returns_none_when_no_port_resolvable(isolated_state_db):
    # Arrange — no allocator claim, cfg.a2a missing.
    from scitex_agent_container._listen import _forward

    cfg = _Cfg(a2a=None)
    # Act
    result = asyncio.run(
        _forward.forward_to_live_runner(cfg, "ghost", "hi", {}, timeout=1.0)
    )
    # Assert
    assert result is None


def test_returns_none_when_port_is_auto_sentinel(isolated_state_db):
    # Arrange — legacy cfg with the "auto" string but no allocator claim.
    from scitex_agent_container._listen import _forward

    cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port="auto"))
    # Act
    result = asyncio.run(
        _forward.forward_to_live_runner(cfg, "ghost", "hi", {}, timeout=1.0)
    )
    # Assert
    assert result is None


def test_returns_none_when_runner_unreachable(isolated_state_db):
    # Arrange — explicit port the kernel will refuse on (nothing bound).
    from scitex_agent_container._listen import _forward

    cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=_free_port()))
    # Act
    result = asyncio.run(
        _forward.forward_to_live_runner(cfg, "ghost", "hi", {}, timeout=2.0)
    )
    # Assert
    assert result is None


def test_explicit_port_from_cfg_reaches_live_runner(isolated_state_db):
    # Arrange — no allocator claim; cfg pins the explicit port.
    from scitex_agent_container._listen import _forward

    async def _run() -> object:
        body = json.dumps({"text": "hello-back"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            return await _forward.forward_to_live_runner(
                cfg, "agent-x", "ping", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert response is not None and response.status_code == 200


def test_successful_text_payload_is_unwrapped(isolated_state_db):
    # Arrange
    from scitex_agent_container._listen import _forward

    async def _run() -> object:
        body = json.dumps({"text": "unwrapped-text"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            return await _forward.forward_to_live_runner(
                cfg, "agent-x", "ping", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert json.loads(response.body)["text"] == "unwrapped-text"


def test_prompt_is_posted_as_text_field(isolated_state_db):
    # Arrange
    from scitex_agent_container._listen import _forward

    captured: dict[str, bytes] = {}

    async def _run() -> None:
        body = json.dumps({"text": "ok"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            await _forward.forward_to_live_runner(
                cfg, "agent-x", "the-prompt", {}, timeout=5.0
            )
            captured["body"] = srv.last_body or b""

    # Act
    asyncio.run(_run())
    # Assert
    assert json.loads(captured["body"].decode())["text"] == "the-prompt"


def test_post_targets_v1_turn_endpoint(isolated_state_db):
    # Arrange
    from scitex_agent_container._listen import _forward

    captured: dict[str, str | None] = {}

    async def _run() -> None:
        body = json.dumps({"text": "ok"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            await _forward.forward_to_live_runner(cfg, "agent-x", "hi", {}, timeout=5.0)
            captured["path"] = srv.last_path

    # Act
    asyncio.run(_run())
    # Assert
    assert captured["path"] == "/v1/turn"


def test_http_error_status_is_propagated(isolated_state_db):
    # Arrange — runner replies 500.
    from scitex_agent_container._listen import _forward

    async def _run() -> object:
        async with _LoopbackHTTP(500, b"boom") as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            return await _forward.forward_to_live_runner(
                cfg, "agent-x", "hi", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert response.status_code == 500


def test_http_error_body_is_wrapped_under_error_key(isolated_state_db):
    # Arrange
    from scitex_agent_container._listen import _forward

    async def _run() -> object:
        async with _LoopbackHTTP(404, b"missing") as srv:
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=srv.port))
            return await _forward.forward_to_live_runner(
                cfg, "agent-x", "hi", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert json.loads(response.body)["error"] == "missing"


def test_allocator_claim_takes_precedence_over_cfg(isolated_state_db):
    # Arrange — allocator has a claim; cfg pins a *different* (dead) port.
    from scitex_agent_container._listen import _forward
    from scitex_agent_container._state import port_allocator

    async def _run() -> object:
        body = json.dumps({"text": "from-allocator"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            port_allocator.claim_port("agent-y", explicit=srv.port)
            cfg = _Cfg(a2a=_A2A(host="127.0.0.1", port=_free_port()))
            return await _forward.forward_to_live_runner(
                cfg, "agent-y", "hi", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert json.loads(response.body)["text"] == "from-allocator"


def test_default_host_is_loopback_when_cfg_omits_host(isolated_state_db):
    # Arrange — cfg has port but no host; default 127.0.0.1 must be used.
    from scitex_agent_container._listen import _forward

    async def _run() -> object:
        body = json.dumps({"text": "ok"}).encode()
        async with _LoopbackHTTP(200, body) as srv:
            cfg = _Cfg(a2a=_A2A(host=None, port=srv.port))
            return await _forward.forward_to_live_runner(
                cfg, "agent-z", "hi", {}, timeout=5.0
            )

    # Act
    response = asyncio.run(_run())
    # Assert
    assert response.status_code == 200
