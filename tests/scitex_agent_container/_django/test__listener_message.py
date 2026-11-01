"""Real HTTP listener/bridge fixtures; never contact a live fleet agent."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from scitex_agent_container._django._control import new_dispatch_id, send_message
from scitex_agent_container._django._remote import RemoteFleet
from scitex_agent_container._listen._forward import post_to_live_runner, push_to_remote_runner

EXCHANGE = "xch_20261102T000000Z_gui_fixture_abcdef"


def receipt():
    return {"exchange_id": EXCHANGE, "status_code": {"kind": "http", "code": 202, "message": f"accepted; poll `/v1/exchanges/{EXCHANGE}`"}}


class Bridge(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        encoded = json.dumps(receipt()).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class Listener(Bridge):
    received = []
    behavior = "accepted"
    bridge_port = 0

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append({"path": self.path, "body": body,
                                  "authenticated": self.headers.get("Authorization") == "Bearer fixture-token"})
        if self.behavior == "refused":
            encoded = b'{"error":"fixture refusal"}'
            self.send_response(403)
        else:
            outcome = asyncio.run(post_to_live_runner(
                f"http://127.0.0.1:{self.bridge_port}/v1/turn", self.bridge_port,
                body["prompt"], timeout=2, dispatch_id=body["options"]["dispatch_id"],
            ))
            if self.behavior == "ambiguous":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            encoded = b'{"delivered":true}' if self.behavior == "invalid" else outcome.payload
            self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def endpoints():
    Bridge.received = []
    Listener.received = []
    Listener.behavior = "accepted"
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    Listener.bridge_port = bridge.server_port
    listener = ThreadingHTTPServer(("127.0.0.1", 0), Listener)
    for server in (bridge, listener):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield RemoteFleet(f"http://127.0.0.1:{listener.server_port}", "fixture-token"), bridge.server_port
    for server in (listener, bridge):
        server.shutdown()
        server.server_close()


def test_dashboard_uses_authenticated_listener_and_preserves_dispatch(endpoints):
    # Arrange
    fleet, _ = endpoints
    dispatch = new_dispatch_id()
    # Act
    result = send_message("http://unreachable.invalid:9/v1/turn", text="synthetic fixture", dispatch_id=dispatch,
                          fleet=fleet, agent_name="fixture-agent")
    # Assert
    assert (result.state == "accepted" and result.exchange_id == EXCHANGE
            and "completion is pending" in result.message
            and Listener.received == [{"path": "/agents/fixture-agent/send", "authenticated": True,
                                       "body": {"type": "prompt", "prompt": "synthetic fixture", "options": {"dispatch_id": dispatch}}}]
            and Bridge.received == [{"text": "synthetic fixture", "dispatch_id": dispatch}])


def test_accepted_dispatch_is_not_sent_again(endpoints):
    # Arrange
    fleet, _ = endpoints
    dispatch = new_dispatch_id()
    send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    # Act
    repeated = send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    # Assert
    assert repeated.state == "replay" and len(Bridge.received) == len(Listener.received) == 1


def test_ambiguous_accepted_outcome_is_not_retried(endpoints):
    # Arrange
    fleet, _ = endpoints
    Listener.behavior = "ambiguous"
    dispatch = new_dispatch_id()
    # Act
    first = send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    repeated = send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    # Assert
    assert first.state == repeated.state == "unknown" and len(Listener.received) == len(Bridge.received) == 1


def test_listener_refusal_never_reaches_turn_bridge(endpoints):
    # Arrange
    fleet, _ = endpoints
    Listener.behavior = "refused"
    # Act
    result = send_message(None, text="synthetic fixture", dispatch_id=new_dispatch_id(), fleet=fleet, agent_name="fixture-agent")
    # Assert
    assert result.state == "refused" and Bridge.received == []


def test_remote_ssh_transport_preserves_dispatch_to_real_loopback_bridge(endpoints, tmp_path, env_save_restore):
    # Arrange
    _, port = endpoints
    shim = tmp_path / "ssh"
    shim.write_text("#!/usr/bin/python3\nimport os,sys\nos.execv('/bin/sh', ['sh','-c',sys.argv[-1]])\n")
    shim.chmod(0o755)
    import os
    env_save_restore.set("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    dispatch = new_dispatch_id()
    # Act
    result = asyncio.run(push_to_remote_runner("fixture-agent", "synthetic fixture", peer_host="fixture.invalid",
                                               port=port, timeout=2, dispatch_id=dispatch))
    # Assert
    assert result.status_code == 202 and Bridge.received == [{"text": "synthetic fixture", "dispatch_id": dispatch}]


def test_malformed_acceptance_is_unknown_and_not_retried(endpoints):
    # Arrange
    fleet, _ = endpoints
    Listener.behavior = "invalid"
    dispatch = new_dispatch_id()
    # Act
    first = send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    repeated = send_message(None, text="synthetic fixture", dispatch_id=dispatch, fleet=fleet, agent_name="fixture-agent")
    # Assert
    assert first.state == repeated.state == "unknown" and len(Bridge.received) == len(Listener.received) == 1


def test_authorized_gui_json_reports_acceptance_and_exchange(client, loopback, env_save_restore):
    # Arrange
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV
    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post("/alpha/message", {"message": "synthetic fixture", "dispatch_id": new_dispatch_id()}, HTTP_ACCEPT="application/json")
    body = response.json()
    # Assert
    assert response.status_code == 200 and body["ok"] and body["state"] == "accepted" and body["exchange_id"] == EXCHANGE


def test_local_listener_forward_threads_dispatch_option(endpoints, pg_schema):
    # Arrange
    from types import SimpleNamespace
    from scitex_agent_container._listen._forward import forward_to_live_runner
    _, port = endpoints
    cfg = SimpleNamespace(a2a=SimpleNamespace(host="127.0.0.1", port=port))
    dispatch = new_dispatch_id()
    # Act
    result = asyncio.run(forward_to_live_runner(cfg, "fixture-local", "synthetic fixture", {"dispatch_id": dispatch}, timeout=2))
    # Assert
    assert result.status_code == 202 and Bridge.received == [{"text": "synthetic fixture", "dispatch_id": dispatch}]


def test_remote_listener_resolution_threads_dispatch_option(endpoints, pg_schema, tmp_path, env_save_restore):
    # Arrange
    from scitex_agent_container._state import state_store
    from scitex_agent_container._listen._forward import forward_to_remote_runner
    _, port = endpoints
    state_store.record_instance_start(name="fixture-remote", host="fixture.invalid", a2a_port=port)
    shim = tmp_path / "ssh"
    shim.write_text("#!/usr/bin/python3\nimport os,sys\nos.execv('/bin/sh', ['sh','-c',sys.argv[-1]])\n")
    shim.chmod(0o755)
    import os
    env_save_restore.set("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    dispatch = new_dispatch_id()
    # Act
    result = asyncio.run(forward_to_remote_runner("fixture-remote", "synthetic fixture", local_host="fixture-local.invalid", timeout=2, dispatch_id=dispatch))
    # Assert
    assert result.status_code == 202 and Bridge.received == [{"text": "synthetic fixture", "dispatch_id": dispatch}]
