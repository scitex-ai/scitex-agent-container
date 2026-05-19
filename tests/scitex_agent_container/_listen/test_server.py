"""HTTP-route coverage for ``_listen.server`` (PA-306 no-mocks).

Drives the real Starlette app through :class:`starlette.testclient.TestClient`
with real fixture files under ``tmp_path`` and real bearer-token auth.
Module-level path roots (registry dir, runtime dir, ``$HOME``) are
re-pointed at the per-test tmp directory by assigning to the module
attributes — that's configuration, not mocking.

The WI-4 cross-host forwarder tests live at the bottom of this
file. The forwarder helper (``_forward_to_remote``) and the
``node_message_send`` route that invokes it both live in
``_listen/server.py``, so this is the canonical mirror per the
PS-204 orphan-test-file rule. The cross-host section uses its own
``cross_host_env`` fixture (broader scope: state.db + peer-tokens
registry) and a distinct ``SHARED_TOKEN`` constant so the
WI-4 loopback tests do not collide with the existing route tests'
``isolated_env`` / ``TOKEN``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import record_lineage

TOKEN = "test-token-abc123"

# WI-4 cross-host forwarder: both apps in the loopback tests run
# with the same listen token — Q4(b) per-host bearer registry: the
# forwarder pulls ``peer-tokens/host-a.token`` (= this value) when
# forwarding to host A. Real deployments mint independent per-host
# tokens.
SHARED_TOKEN = "test-token-wi4"


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Point every on-disk root at ``tmp_path`` for the duration of one test."""
    saved_home = os.environ.get("HOME")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_yaml_env = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"

    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, val in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml_env),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.fixture
def client(isolated_env):
    """Real Starlette TestClient against ``create_app`` with bearer auth."""
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_spec(home: Path, name: str, workdir: str = "/tmp") -> Path:
    """Write a minimal v3 spec.yaml that ``load_config`` will accept."""
    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "claude": {"model": "claude-sonnet-4-5"},
            "workdir": workdir,
        },
    }
    p = spec_dir / "spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


# --- Health (public) ------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_ok_true(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.json()["ok"] is True

    def test_health_works_without_auth(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.status_code == 200

    def test_health_advertises_service_name(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        body = client.get(url).json()
        # Assert
        assert body["service"] == "sac-listen"


# --- Auth middleware ------------------------------------------------------


class TestBearerAuthMiddleware:
    def test_missing_token_returns_401(self, client):
        # Arrange
        url = "/agents"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.status_code == 401

    def test_missing_token_error_body_explains(self, client):
        # Arrange
        url = "/agents"
        # Act
        body = client.get(url).json()
        # Assert
        assert "missing bearer token" in body["error"]

    def test_wrong_token_returns_403(self, client):
        # Arrange
        headers = {"authorization": "Bearer nope"}
        # Act
        resp = client.get("/agents", headers=headers)
        # Assert
        assert resp.status_code == 403

    def test_wrong_token_error_body_explains(self, client):
        # Arrange
        headers = {"authorization": "Bearer nope"}
        # Act
        body = client.get("/agents", headers=headers).json()
        # Assert
        assert "invalid bearer token" in body["error"]

    def test_malformed_authorization_header_returns_401(self, client):
        # Arrange
        headers = {"authorization": "Basic xyz"}
        # Act
        resp = client.get("/agents", headers=headers)
        # Assert
        assert resp.status_code == 401

    def test_valid_token_passes_through(self, client, auth_headers):
        # Arrange
        url = "/agents"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 200


# --- GET /agents (list) ---------------------------------------------------


class TestListAgents:
    def test_empty_registry_returns_empty_list(self, client, auth_headers):
        # Arrange
        url = "/agents"
        # Act
        body = client.get(url, headers=auth_headers).json()
        # Assert
        assert body == {"agents": []}

    def test_registered_agent_appears_in_list(self, client, auth_headers, isolated_env):
        # Arrange
        r = _reg.Registry()
        r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
        # Act
        body = client.get("/agents", headers=auth_headers).json()
        # Assert
        assert body["agents"][0]["name"] == "alpha"


# --- GET /agents/<name>/status -------------------------------------------


class TestAgentStatus:
    def test_unknown_agent_returns_404(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/status"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_unknown_agent_body_has_error(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/status"
        # Act
        body = client.get(url, headers=auth_headers).json()
        # Assert
        assert "error" in body

    def test_known_agent_returns_name(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert body["name"] == "beta"

    def test_known_agent_returns_state_dir(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert "beta" in body["state_dir"]


# --- POST /agents/<name>/send --------------------------------------------


class TestAgentSendValidation:
    def test_non_json_body_returns_400(self, client, auth_headers):
        # Arrange
        headers = {**auth_headers, "content-type": "application/json"}
        # Act
        resp = client.post("/agents/x/send", content=b"not-json", headers=headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_type_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "weird"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_missing_prompt_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_empty_prompt_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt", "prompt": ""}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_agent_for_prompt_returns_404(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt", "prompt": "hi"}
        # Act
        resp = client.post("/agents/no-such/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_prompt_without_session_id_returns_409(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        _write_spec(isolated_env, "gamma")
        # Act
        resp = client.post(
            "/agents/gamma/send",
            json={"type": "prompt", "prompt": "hello"},
            headers=auth_headers,
        )
        # Assert
        assert resp.status_code == 409


class TestAgentSendKey:
    def test_unknown_key_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "key", "key": "F13"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_key_error_lists_supported(self, client, auth_headers):
        # Arrange
        body = {"type": "key", "key": "F13"}
        # Act
        out = client.post("/agents/x/send", json=body, headers=auth_headers).json()
        # Assert
        assert "ESC" in out["error"]

    def test_key_without_pid_file_returns_404(self, client, auth_headers, isolated_env):
        # Arrange
        body = {"type": "key", "key": "ESC"}
        # Act
        resp = client.post("/agents/ghost/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_key_with_pid_file_signals_running_process(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        import signal

        prev = signal.signal(signal.SIGINT, lambda *_: None)
        state_dir = _ss.state_dir_for("delta")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pid").write_text(str(os.getpid()))
        # Act
        try:
            resp = client.post(
                "/agents/delta/send",
                json={"type": "key", "key": "SIGINT"},
                headers=auth_headers,
            )
        finally:
            signal.signal(signal.SIGINT, prev)
        # Assert
        assert resp.status_code == 200

    def test_key_with_malformed_pid_returns_500(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        state_dir = _ss.state_dir_for("epsilon")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pid").write_text("not-a-pid")
        # Act
        resp = client.post(
            "/agents/epsilon/send",
            json={"type": "key", "key": "ESC"},
            headers=auth_headers,
        )
        # Assert
        assert resp.status_code == 500


# --- POST /agents (start) -------------------------------------------------


class TestAgentsStartValidation:
    def test_non_json_body_returns_400(self, client, auth_headers):
        # Arrange
        headers = {**auth_headers, "content-type": "application/json"}
        # Act
        resp = client.post("/agents", content=b"xxx", headers=headers)
        # Assert
        assert resp.status_code == 400

    def test_missing_name_returns_400(self, client, auth_headers):
        # Arrange
        body: dict = {}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_empty_name_returns_400(self, client, auth_headers):
        # Arrange
        body = {"name": ""}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_non_dict_returns_400(self, client, auth_headers):
        # Arrange
        body = {"name": "z", "spec": "not-a-dict"}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_wrong_api_version_returns_400(self, client, auth_headers):
        # Arrange
        body = {
            "name": "z",
            "spec": {"apiVersion": "wrong", "kind": "Agent"},
        }
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_wrong_kind_returns_400(self, client, auth_headers):
        # Arrange
        body = {
            "name": "z",
            "spec": {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Other",
            },
        }
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400


# --- GET /agents/<name>/.well-known/agent-card.json ----------------------


class TestAgentCard:
    def test_unknown_agent_returns_404(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/.well-known/agent-card.json"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_known_agent_card_returns_200(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        resp = client.get(
            "/agents/zeta/.well-known/agent-card.json", headers=auth_headers
        )
        # Assert
        assert resp.status_code == 200

    def test_known_agent_card_body_is_dict(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        body = client.get(
            "/agents/zeta/.well-known/agent-card.json", headers=auth_headers
        ).json()
        # Assert
        assert isinstance(body, dict)


# --- DELETE /agents/<name> -----------------------------------------------


class TestAgentDelete:
    def test_no_pid_file_returns_404(self, client, auth_headers, isolated_env):
        # Arrange
        url = "/agents/no-such"
        # Act
        resp = client.delete(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_malformed_pid_returns_500(self, client, auth_headers, isolated_env):
        # Arrange
        sd = _ss.state_dir_for("bad")
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "pid").write_text("not-an-int")
        # Act
        resp = client.delete("/agents/bad", headers=auth_headers)
        # Assert
        assert resp.status_code == 500

    def test_live_pid_returns_stopped_true(self, client, auth_headers, isolated_env):
        # Arrange
        import signal

        prev = signal.signal(signal.SIGTERM, lambda *_: None)
        sd = _ss.state_dir_for("livepid")
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "pid").write_text(str(os.getpid()))
        # Act
        try:
            body = client.delete("/agents/livepid", headers=auth_headers).json()
        finally:
            signal.signal(signal.SIGTERM, prev)
        # Assert
        assert body["stopped"] is True


# --- GET /agents/<name>/tail (SSE) ---------------------------------------


class TestAgentTail:
    def test_missing_session_jsonl_no_follow_returns_404(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        url = "/agents/missing/tail?follow=false"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_tail_streams_recorded_lines(self, client, auth_headers, isolated_env):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text(
            '{"ts": "2025-01-01T00:00:00", "msg": "hello"}\n'
            '{"ts": "2025-01-01T00:00:01", "msg": "world"}\n',
            encoding="utf-8",
        )
        # Act
        resp = client.get("/agents/tailer/tail?follow=false", headers=auth_headers)
        # Assert
        assert "hello" in resp.text

    def test_tail_content_type_is_event_stream(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer2"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text('{"msg": "x"}\n', encoding="utf-8")
        # Act
        resp = client.get("/agents/tailer2/tail?follow=false", headers=auth_headers)
        # Assert
        assert "text/event-stream" in resp.headers["content-type"]

    def test_tail_with_since_filters_old_records(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer3"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text(
            '{"ts": "2020-01-01T00:00:00", "msg": "old"}\n'
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n',
            encoding="utf-8",
        )
        # Act
        resp = client.get(
            "/agents/tailer3/tail?follow=false&since=2025-01-01T00:00:00",
            headers=auth_headers,
        )
        # Assert
        assert "old" not in resp.text


# --- Unknown route --------------------------------------------------------


class TestUnknownRoute:
    def test_unknown_path_returns_404(self, client, auth_headers):
        # Arrange
        url = "/v1/nope"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WI-4 — cross-host forwarder on ``sac listen`` (handoff §4).
#
# Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4):
#
#   Acceptance: a node on host B sends to one on host A; the event
#   arrives, ACL-checked at A.
#
# The end-to-end tests drive two real ``uvicorn`` instances on
# loopback ports to simulate the two-host topology:
#
#   * "host A" — owns the target. ACL is gated here.
#   * "host B" — the forwarding entry point. Records the target's
#     instance under ``host="host-a"`` so the resolver routes there.
#
# A POST to host B's ``message:send`` for that target arrives on
# host A's broker. The Q4(b) per-host bearer registry is exercised
# by the missing-peer-token loud-502 tests at the bottom.
#
# No mocks (handoff §0): real SQLite + real ``uvicorn``.
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_env(tmp_path: Path):
    """Isolated state.db + runtime/registry roots + peer-token for host A.

    Broader scope than ``isolated_env`` above (which only redirects
    HOME / registry / runtime): this fixture also wires up state.db
    and seeds the WI-4 Q4(b) per-host bearer registry under
    ``~/.scitex/agent-container/peer-tokens/`` so the forwarder can
    authenticate at host A.
    """
    # Arrange
    db = tmp_path / "state.db"
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    # WI-4 Q4(b): seed the per-host bearer registry. The forwarder
    # pulls ``peer-tokens/host-a.token`` to authenticate when
    # forwarding to host A. ``$HOME`` is already tmp_path so the
    # default ``~/.scitex/agent-container/peer-tokens/`` lands here.
    write_peer_token(peer_host="host-a", token=SHARED_TOKEN)
    try:
        yield {"db": db, "tmp": tmp_path}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin up uvicorn on a loopback port. The app's
    ``local_host`` identity is configured at ``create_app`` time
    (see :func:`scitex_agent_container._listen.server.create_app`).
    """
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", ws="none"
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    import time as _time

    deadline = _time.monotonic() + 5.0
    while not server.started:
        if _time.monotonic() > deadline:
            raise RuntimeError("uvicorn loopback did not start in 5s")
        _time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        t.join(timeout=5.0)


def _send_payload(text: str, *, from_agent: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": {"from_agent": from_agent},
        },
    }


def test_cross_host_send_forwards_to_target_host(cross_host_env) -> None:
    """End-to-end: a POST to host B's ``message:send`` for a target
    pinned to host A arrives on host A's broker.
    """
    # Arrange
    db = cross_host_env["db"]
    # Register the target as a live instance on host-a.
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=0, db_path=db
    )
    # Permitted-peer is registered as a child of root, so is alice;
    # they share a group and ACL allows the send.
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)

    host_a_port = _free_port()
    host_b_port = _free_port()

    # Bind the actual port for host A onto the instances row so the
    # resolver routes to the right loopback.
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )

    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            # Subscribe on host A as alice.
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=5.0) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=5.0)
                # Now stand up host B and post to it. WI-4 forwarder
                # on host B should resolve alice→host-a and forward.
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=5.0) as ac:
                        resp = await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(
                                "hi from b", from_agent="permitted-peer"
                            ),
                            headers={
                                "authorization": f"Bearer {SHARED_TOKEN}"
                            },
                        )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"forward returned {resp.status_code}: {resp.text!r}"
                    )
                await asyncio.wait_for(sub, timeout=5.0)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    # Act
    event = asyncio.run(driver())
    # Assert
    assert event.get("content") == "hi from b"


def test_cross_host_forward_preserves_from_agent_metadata(cross_host_env) -> None:
    """The forwarded event keeps the original ``from_agent`` so
    host A's ACL can gate on the real sender, not the forwarding
    host's identity.
    """
    # Arrange
    db = cross_host_env["db"]
    state_db.record_instance_start(
        name="alice", host="host-a", a2a_port=0, db_path=db
    )
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    host_a_port = _free_port()
    host_b_port = _free_port()
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )
    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=5.0) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=5.0)
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=5.0) as ac:
                        await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(
                                "x", from_agent="permitted-peer"
                            ),
                            headers={
                                "authorization": f"Bearer {SHARED_TOKEN}"
                            },
                        )
                await asyncio.wait_for(sub, timeout=5.0)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    # Act
    event = asyncio.run(driver())
    # Assert
    assert event.get("from_agent") == "permitted-peer"


# ---------------------------------------------------------------------------
# WI-4 Q4(b): missing peer-token → loud 502 (handoff §0 — no silent drop).
# ---------------------------------------------------------------------------


@pytest.fixture
def missing_peer_token_response(tmp_path: Path):
    """Drive a forwarder POST with NO peer-token written for the
    destination host, so the forwarder must fall through to the loud
    502 path. Yielded value is the live ``httpx.Response`` so each
    test can assert one aspect of the failure shape.
    """
    # Arrange — fresh tmp env, NO peer-token for host-z.
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    db = tmp_path / "state.db"
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    state_db.record_instance_start(
        name="alice", host="host-z", a2a_port=9999, db_path=db
    )
    app_local = create_app(token=SHARED_TOKEN, local_host="host-b")

    try:
        with TestClient(app_local) as client:
            r = client.post(
                "/agents/alice/message:send",
                json=_send_payload("hi", from_agent="permitted-peer"),
                headers={"authorization": f"Bearer {SHARED_TOKEN}"},
            )
        yield r
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def test_cross_host_forward_502_when_peer_token_missing(
    missing_peer_token_response,
) -> None:
    """When forwarding to a host whose bearer is NOT in
    ``peer-tokens/``, the forwarder fails loudly with 502
    (handoff §0 — no silent drop).
    """
    # Arrange
    r = missing_peer_token_response
    # Act
    status = r.status_code
    # Assert
    assert status == 502, r.text


def test_cross_host_forward_502_body_names_missing_host(
    missing_peer_token_response,
) -> None:
    """The 502 body names the specific peer host so the operator
    sees which ``sac host add-peer`` to run."""
    # Arrange
    r = missing_peer_token_response
    # Act
    err = r.json().get("error", "")
    # Assert
    assert "host-z" in err


def test_cross_host_forward_502_body_carries_add_peer_fix(
    missing_peer_token_response,
) -> None:
    """The 502 body advertises the ``sac host add-peer`` remediation
    so the loud failure points at the fix."""
    # Arrange
    r = missing_peer_token_response
    # Act
    err = r.json().get("error", "")
    # Assert
    assert "sac host add-peer" in err
