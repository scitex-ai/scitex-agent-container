"""MCP prompt dispatch through a real private HTTP authority boundary."""

from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from scitex_agent_container._mcp._tools._agent import agent_send

EXCHANGE = "xch_20261002T230000Z_synthetic_abcdef"


def accepted_turn() -> dict:
    return {
        "exchange_id": EXCHANGE,
        "status_code": {
            "kind": "http",
            "code": 202,
            "message": "queued; poll `/agents/retained-peer/exchanges/<exchange>`",
        },
        "receipt": {"state": "pending", "final": False, "source": "synthetic-host"},
    }


@contextmanager
def host_endpoint(
    env_save_restore, status: int, response: dict, *, health_response=None
):
    """An authenticated loopback fixture; no runner, account or Store exists."""

    class Calls(list):
        health_reads = None

    calls = Calls()
    calls.health_reads = []
    health_response = (
        health_response
        if health_response is not None
        else {
            "ok": True,
            "service": "sac-listen",
            "v": 1,
            "capabilities": {
                "start_session": {
                    "protocol": "sac.start-session/v1",
                    "modes": ["continue", "resume", "fresh"],
                }
            },
        }
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            calls.health_reads.append((self.path, self.headers.get("Authorization")))
            raw = json.dumps(health_response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body, self.headers.get("Authorization")))
            code = (
                status
                if self.headers.get("Authorization") == "Bearer synthetic-host"
                else 401
            )
            raw = json.dumps(response).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_save_restore.set("APPTAINER_CONTAINER", "/synthetic/fixture.sif")
    env_save_restore.set(
        "SAC_LISTEN_BASE_URL", f"http://127.0.0.1:{server.server_port}"
    )
    env_save_restore.set("SAC_LISTEN_BEARER", "synthetic-host")
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_container_prompt_uses_host_acl_endpoint_once(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 202, accepted_turn()) as calls:
        # Act
        agent_send("retained-peer", prompt="bounded synthetic work")
    # Assert
    assert calls == [
        (
            "/agents/retained-peer/send",
            {"prompt": "bounded synthetic work"},
            "Bearer synthetic-host",
        )
    ]


def test_mcp_queued_exchange_does_not_claim_admission(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 202, accepted_turn()):
        # Act
        result = agent_send("retained-peer", prompt="synthetic", wait=True)
    # Assert
    assert (result["status"], result["exchange_id"], result["receipt"]) == (
        "pending",
        EXCHANGE,
        {"state": "pending", "final": False, "source": "synthetic-host"},
    )


@pytest.mark.parametrize("status", (401, 403, 404, 409, 502))
def test_mcp_host_refusal_has_no_local_fallback(env_save_restore, status):
    # Arrange
    with host_endpoint(env_save_restore, status, {"error": "host refused"}) as calls:
        # Act
        result = agent_send("retained-peer", prompt="synthetic")
    # Assert
    assert (result["status"], result["http_status"], len(calls)) == ("error", status, 1)


@pytest.mark.parametrize(
    "body",
    (
        pytest.param({**accepted_turn(), "exchange_id": None}, id="missing_exchange"),
        pytest.param(
            {
                **accepted_turn(),
                "status_code": {"kind": "http", "code": 200, "message": "completed"},
            },
            id="wrong_status",
        ),
        pytest.param([], id="not_object"),
        pytest.param(
            {
                **accepted_turn(),
                "status_code": {"kind": "http", "code": 202, "message": "queued"},
            },
            id="missing_probe",
        ),
    ),
)
def test_mcp_malformed_202_never_becomes_dispatch(env_save_restore, body):
    # Arrange
    with host_endpoint(env_save_restore, 202, body) as calls:
        # Act
        result = agent_send("retained-peer", prompt="synthetic")
    # Assert
    assert (result["status"], len(calls)) == ("error", 1)


def test_mcp_container_missing_host_authority_is_unknown(env_save_restore):
    # Arrange
    env_save_restore.set("APPTAINER_CONTAINER", "/synthetic/fixture.sif")
    env_save_restore.set("SAC_LISTEN_BASE_URL", "")
    # Act
    result = agent_send("retained-peer", prompt="synthetic")
    # Assert
    assert result["diagnosis"]["registry_status"] == "unknown_host_authority"


def test_mcp_host_options_use_the_existing_host_options_field(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 202, accepted_turn()) as calls:
        # Act
        agent_send(
            "retained-peer", prompt="synthetic", model="declared-model", max_turns=1
        )
    # Assert
    assert calls[0][1] == {
        "prompt": "synthetic",
        "options": {"model": "declared-model", "max_turns": 1},
    }
