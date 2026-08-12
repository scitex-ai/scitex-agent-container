"""Tests for the host-brokered peer lookup on the send read path.

The bug being pinned
--------------------
Inside a container ``$HOME`` is ``/home/agent`` and
``SCITEX_AGENT_CONTAINER_STATE_DB`` points at a private per-agent bridge DB,
so ``open_db(None)`` resolves to a store with no rows for any other agent.
``send_to_agent`` read that empty store and reported EVERY peer as dead.
Measured 2026-07-14 from inside the SIF::

    send_to_agent("scitex-scholar")
      -> error "agent 'scitex-scholar' not running"
         registry_status="stopped", pid=null, a2a_port=null, boot_complete=false

while the host's registry held ``pid=1777985  a2a_port=19037  ended_at=NULL``
for that same agent, at that same moment.

What these tests hold down
--------------------------
1. In a container the lookup is brokered to the host and a LIVE peer is
   reachable again (the regression that started this).
2. "I could not check" is NEVER rendered as "the agent is stopped" — an
   unreachable broker, an ACL refusal and a 5xx all yield UNKNOWN.
3. Hints are never promoted to death verdicts: a stale ``startup_failed``
   marker does not hide a live port, and an UNBOUND ``/v1/turn`` port does
   not make ``pid_alive`` / ``boot_complete`` false. (Measured 2026-07-14:
   only 5 of 47 live fleet agents had ``/v1/turn`` bound at all — a
   port-bind death gate would have condemned 41 healthy agents, whose
   "remedy" ``--force --fresh`` is destructive.)
4. On a BARE HOST nothing is brokered — the local read path is untouched.

PA-306 / STX-NM002: no mocks, no monkeypatch. A REAL in-process HTTP server
stands in for the host ``sac listen`` (same shape as
``test__send_in_sif_auto_fallback.py``), reached over the REAL urllib stack,
and the REAL ``send_to_agent`` is driven end to end. State.db is redirected
to a tmp sandbox so no test ever reads the live fleet registry.
STX-TQ007: one fact per test. STX-TQ002: AAA.
"""

from __future__ import annotations

import http.server
import json
import socket
import socketserver
import threading
import time
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._send import send_to_agent
from scitex_agent_container.cli_pkg._send_broker import (
    PeerLookupUnavailable,
    lookup_peer_via_host,
    resolve_send_endpoint_via_host,
    should_broker_peer_lookup,
)

_LOCAL_HOST = "test-host"


# "A port nothing is listening on" comes from the shared ``dead_port`` fixture
# (tests/scitex_agent_container/_helpers/ports.py, wired in tests/conftest.py):
# bound WITHOUT listening, so a connect is refused, and HELD, so no other test
# or xdist worker can bind it mid-test. The helper that used to live here
# released the port before the probe ran — see that module for the flake.


def _status_body(**over) -> bytes:
    """A ``GET /agents/<name>/status`` body in the host's real shape."""
    body = {
        "name": "peer",
        "spec_path": "/specs/peer.yaml",
        "workdir": "/work",
        "session_id": None,
        "state_dir": "/state/peer",
        "a2a_port": None,
        "turn_url": None,
        "role": "worker",
    }
    body.update(over)
    return json.dumps(body).encode()


@pytest.fixture
def fake_host_listen(env_save_restore, tmp_path):
    """A REAL HTTP server standing in for the host ``sac listen``.

    Also puts the process in the in-container state the fix keys off
    (``APPTAINER_CONTAINER`` + ``SAC_LISTEN_BASE_URL``) and redirects
    state.db to a tmp sandbox, so the blind local read the bug came from is
    empty here too — exactly as it is inside a real SIF.
    """
    responses: list[tuple[int, bytes]] = []
    captured: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured.append(self.path)
            status, body = (
                responses.pop(0) if responses else (200, _status_body())
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args, **kw):  # noqa: ARG002
            return

    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env_save_restore.set("SAC_LISTEN_BASE_URL", f"http://127.0.0.1:{port}")
    env_save_restore.set("SAC_LISTEN_BEARER", "test-bearer")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    env_save_restore.set("SAC_HOST", _LOCAL_HOST)
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
    )

    class _Ctl:
        # The server's own port is guaranteed BOUND — hand it out as a live
        # agent's a2a port so the reachability probe measures a real socket.
        live_port = port

        @property
        def captured(self) -> list[str]:
            return captured

        def enqueue(self, status: int, body: bytes) -> None:
            responses.append((status, body))

    try:
        yield _Ctl()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.fixture
def fresh_lead_creds_path(tmp_path) -> Path:
    """Fresh OAuth creds so ``preflight_send_creds`` passes deterministically."""
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {"claudeAiOauth": {"expiresAt": int((time.time() + 3600) * 1000)}}
        )
    )
    return creds


# ---------------------------------------------------------------------------
# should_broker_peer_lookup — the bare-host path must stay untouched
# ---------------------------------------------------------------------------


def test_bare_host_does_not_broker(env_save_restore):
    # Arrange — no SIF markers: an ordinary host shell.
    env_save_restore.delete("APPTAINER_CONTAINER")
    env_save_restore.delete("SINGULARITY_CONTAINER")
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878")
    # Act
    brokering = should_broker_peer_lookup()
    # Assert — bare host keeps the local read path.
    assert brokering is False


def test_in_sif_without_listen_url_does_not_broker(env_save_restore):
    # Arrange — a stale SINGULARITY_CONTAINER with no host listen to talk to
    # (the sac-from-sac / bare-host footgun status_cmds hit in PR#316).
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    # Act
    brokering = should_broker_peer_lookup()
    # Assert — no URL to broker to → do not broker.
    assert brokering is False


def test_in_sif_with_listen_url_brokers(fake_host_listen):
    # Arrange — fixture puts us in the real in-container state.
    # Act
    brokering = should_broker_peer_lookup()
    # Assert
    assert brokering is True


# ---------------------------------------------------------------------------
# lookup_peer_via_host — the host's answer
# ---------------------------------------------------------------------------


def test_lookup_returns_the_port_the_host_reports(fake_host_listen):
    # Arrange — the host knows this agent holds port 19037 (scholar's real one).
    fake_host_listen.enqueue(
        200,
        _status_body(
            a2a_port=19037, turn_url="http://ywata-note-win:19037/v1/turn"
        ),
    )
    # Act
    peer = lookup_peer_via_host("scitex-scholar")
    # Assert — the port the blind local read could never see.
    assert peer.a2a_port == 19037


def test_lookup_uses_the_agent_status_door(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, _status_body(a2a_port=1))
    # Act
    lookup_peer_via_host("scitex-scholar")
    # Assert — the SAME door agent_status uses; no second mechanism.
    assert fake_host_listen.captured == ["/agents/scitex-scholar/status"]


def test_lookup_reports_host_404_as_not_known(fake_host_listen):
    # Arrange — the host, which sees the whole fleet, has no such agent.
    fake_host_listen.enqueue(404, b'{"error":"no such agent"}')
    # Act
    peer = lookup_peer_via_host("ghost")
    # Assert — the one definitive negative.
    assert peer.known is False


def test_stale_startup_failed_marker_does_not_hide_the_live_port(fake_host_listen):
    # Arrange — scitex-writer, measured 2026-07-14: a ~2-day-old
    # startup_failed marker while the agent was alive and answering a2a.
    fake_host_listen.enqueue(
        200,
        _status_body(
            status="startup_failed",
            a2a_port=19014,
            turn_url="http://ywata-note-win:19014/v1/turn",
        ),
    )
    # Act
    peer = lookup_peer_via_host("scitex-writer")
    # Assert — the marker is a HINT; it must not suppress the live endpoint.
    assert peer.a2a_port == 19014


def test_startup_failed_is_carried_as_a_hint_only(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(200, _status_body(status="startup_failed", a2a_port=1))
    # Act
    peer = lookup_peer_via_host("scitex-writer")
    # Assert — surfaced for the operator, but it is not the verdict field.
    assert peer.host_status == "startup_failed"


# ---------------------------------------------------------------------------
# Could-not-ask → UNKNOWN. Never "stopped".
# ---------------------------------------------------------------------------


def test_unreachable_broker_raises_rather_than_reporting_stopped(
    env_save_restore, tmp_path, dead_port
):
    # Arrange — in a SIF, but the host listen is down.
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    env_save_restore.set(
        "SAC_LISTEN_BASE_URL", dead_port.url("")
    )
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
    )

    # Act
    def _lookup():
        lookup_peer_via_host("scitex-scholar")

    # Assert — refuses to answer rather than guessing "dead".
    with pytest.raises(PeerLookupUnavailable):
        _lookup()


def test_acl_deny_raises_rather_than_reporting_stopped(fake_host_listen):
    # Arrange — the host refuses to tell us (403). We were prevented from
    # learning the state; that is not the same as learning it is stopped.
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"cross-lineage"}'
    )

    # Act
    def _lookup():
        lookup_peer_via_host("scitex-scholar")

    # Assert
    with pytest.raises(PeerLookupUnavailable):
        _lookup()


def test_acl_deny_preserves_the_hosts_kind(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        403, b'{"error":"ACL deny","kind":"acl_deny","reason":"cross-lineage"}'
    )
    captured: Exception | None = None
    # Act
    try:
        lookup_peer_via_host("scitex-scholar")
    except PeerLookupUnavailable as exc:
        captured = exc
    # Assert — the gate's refusal is surfaced verbatim, never worked around.
    assert getattr(captured, "kind", None) == "acl_deny"


# ---------------------------------------------------------------------------
# resolve_send_endpoint_via_host — provenance is visible in the result
# ---------------------------------------------------------------------------


def test_resolved_endpoint_is_tagged_host_broker(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        200, _status_body(a2a_port=19037, turn_url="http://peer-host:19037/v1/turn")
    )
    # Act
    endpoint, _peer = resolve_send_endpoint_via_host(
        "scitex-scholar", current_host=_LOCAL_HOST
    )
    # Assert — a reader can always tell the fleet registry from a blind read.
    assert endpoint.source == "host_broker"


def test_resolved_endpoint_carries_the_peers_host(fake_host_listen):
    # Arrange
    fake_host_listen.enqueue(
        200, _status_body(a2a_port=19037, turn_url="http://peer-host:19037/v1/turn")
    )
    # Act
    endpoint, _peer = resolve_send_endpoint_via_host(
        "scitex-scholar", current_host=_LOCAL_HOST
    )
    # Assert
    assert endpoint.host == "peer-host"


# ---------------------------------------------------------------------------
# send_to_agent — the regression, driven end to end through the real function
# ---------------------------------------------------------------------------


def test_live_peer_is_reachable_from_inside_a_container(
    fake_host_listen, fresh_lead_creds_path
):
    # Arrange — the host reports a live agent on a port that IS bound.
    # Before the fix this returned: error "agent 'peer' not running".
    port = fake_host_listen.live_port
    fake_host_listen.enqueue(
        200,
        _status_body(a2a_port=port, turn_url=f"http://{_LOCAL_HOST}:{port}/v1/turn"),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — the fleet can talk to itself again.
    assert result["status"] == "dispatched"


def test_dispatch_targets_the_port_the_host_reported(
    fake_host_listen, fresh_lead_creds_path
):
    # Arrange
    port = fake_host_listen.live_port
    fake_host_listen.enqueue(
        200,
        _status_body(a2a_port=port, turn_url=f"http://{_LOCAL_HOST}:{port}/v1/turn"),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — the endpoint came from the fleet registry, not the empty local DB.
    assert result["a2a_port"] == port


def test_unreachable_broker_never_reports_the_peer_stopped(
    env_save_restore, tmp_path, fresh_lead_creds_path, dead_port
):
    # Arrange — in a SIF; the host listen is down, so we cannot check.
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    env_save_restore.set("SAC_HOST", _LOCAL_HOST)
    env_save_restore.set(
        "SAC_LISTEN_BASE_URL", dead_port.url("")
    )
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — UNKNOWN, not dead. "I could not check" must never render as
    # "the agent is stopped" — that is the bug, and its remedy is destructive.
    assert result["diagnosis"]["registry_status"].startswith("unknown")


def test_unreachable_broker_names_the_broker_as_the_failure(
    env_save_restore, tmp_path, fresh_lead_creds_path, dead_port
):
    # Arrange
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/test.sif")
    env_save_restore.set("SAC_HOST", _LOCAL_HOST)
    env_save_restore.set(
        "SAC_LISTEN_BASE_URL", dead_port.url("")
    )
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "state.db")
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — fail honestly: the BROKER is what is unreachable, not the agent.
    assert "broker is unreachable" in result["error"]


def test_unbound_turn_port_still_reports_the_agent_running(
    fake_host_listen, fresh_lead_creds_path, dead_port
):
    # Arrange — the live-fleet norm: the host holds a port claim but nothing
    # listens on it (41 of 47 agents, measured 2026-07-14). Those agents are
    # alive and answer over the a2a subscriber channel.
    fake_host_listen.enqueue(
        200,
        _status_body(
            a2a_port=dead_port(),
            turn_url=f"http://{_LOCAL_HOST}:1/v1/turn",
        ),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — an unbound port is a TRANSPORT fact, not a death certificate.
    assert result["diagnosis"]["registry_status"] == "running"


def test_unbound_turn_port_does_not_fabricate_a_dead_pid(
    fake_host_listen, fresh_lead_creds_path, dead_port
):
    # Arrange
    fake_host_listen.enqueue(
        200,
        _status_body(
            a2a_port=dead_port(),
            turn_url=f"http://{_LOCAL_HOST}:1/v1/turn",
        ),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — UNKNOWN (None), never False. A False here trips the caller's
    # pid_alive death gate and condemns a healthy agent.
    assert result["diagnosis"]["pid_alive"] is None


def test_unbound_turn_port_does_not_fabricate_a_failed_boot(
    fake_host_listen, fresh_lead_creds_path, dead_port
):
    # Arrange
    fake_host_listen.enqueue(
        200,
        _status_body(
            a2a_port=dead_port(),
            turn_url=f"http://{_LOCAL_HOST}:1/v1/turn",
        ),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — the host route carries no heartbeat, so boot state is UNKNOWN.
    assert result["diagnosis"]["boot_complete"] is None


def test_unbound_turn_port_error_does_not_claim_the_agent_crashed(
    fake_host_listen, fresh_lead_creds_path, dead_port
):
    # Arrange
    fake_host_listen.enqueue(
        200,
        _status_body(
            a2a_port=dead_port(),
            turn_url=f"http://{_LOCAL_HOST}:1/v1/turn",
        ),
    )
    # Act
    result = send_to_agent(
        "peer", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert — the old wording ("it is not booted or the sidecar crashed") was
    # a death verdict whose remedy destroys a working agent.
    assert "crashed" not in result["error"]


def test_host_404_is_reported_as_not_found_not_stopped(
    fake_host_listen, fresh_lead_creds_path
):
    # Arrange — the host genuinely has no agent by this name.
    fake_host_listen.enqueue(404, b'{"error":"no such agent"}')
    # Act
    result = send_to_agent(
        "ghost", "hello", wait=False, lead_creds_path=fresh_lead_creds_path
    )
    # Assert
    assert result["diagnosis"]["registry_status"] == "not_found"
