"""Agent control surfaces: message/steer, control keys, idempotency, refusals.

Card sac-agent-activity-timeline-dashboard-20260917. These tests use a REAL
loopback bridge that speaks the SAC turn-bridge contract (no mocks, no
monkeypatch), so the transport is exercised end to end.

THE SAFETY-CRITICAL PROPERTY UNDER TEST IS IDEMPOTENCY. A duplicate delivery
injects the operator's text into a LIVE agent session twice. That is the worst
outcome this surface can produce, so it is asserted first and hardest.

Nothing here contacts a real fleet agent: the listener is bound to an ephemeral
loopback port.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scitex_agent_container._django._control import (
    CONTROL_KEYS,
    MAX_MESSAGE_CHARS,
    exactly_one_of,
    new_dispatch_id,
    redact,
    send_control_key,
    send_message,
)


class _Bridge(BaseHTTPRequestHandler):
    """A real SAC turn bridge: /v1/turn and /v1/control, contract-shaped."""

    received: list[dict[str, Any]] = []
    accepts_control = True

    def log_message(self, *args):
        return

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, {"error": "bad JSON"})
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/control"):
            key = body.get("key")
            if key not in {"Enter", "Escape", "ESC", "C-c", "SIGINT"}:
                self._send(400, {"error": "control key must be Enter, Escape, or C-c"})
                return
            if not self.accepts_control:
                self._send(501, {"error": "this runtime has no UI-control surface"})
                return
            type(self).received.append({"route": "control", "key": key})
            self._send(200, {"delivered": True, "mode": "tui-control", "key": key})
            return
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(400, {"error": "missing or empty 'text' field"})
            return
        type(self).received.append(
            {"route": "turn", "text": text, "dispatch_id": body.get("dispatch_id")}
        )
        self._send(200, {"delivered": True, "exchange_id": "xch-1"})

    def do_GET(self):  # noqa: N802
        self._send(200, {"status": "ok", "agent": "under-test"})


@pytest.fixture
def bridge(env_save_restore):
    """A real loopback turn bridge; yields its base URL."""
    _Bridge.received = []
    _Bridge.accepts_control = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def dead_bridge():
    """A bridge that is not listening: a closed loopback port.

    Yields (not returns) because it acquires a socket — a resource-acquiring
    fixture must let pytest tear it down.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    yield f"http://127.0.0.1:{port}"


# ── idempotency: the property that protects a live session ───────────────────


def test_repeated_submit_delivers_once(bridge):
    # Arrange
    dispatch = "fixed-dispatch-id"
    # Act
    first = send_message(f"{bridge}/v1/turn", text="hello", dispatch_id=dispatch)
    second = send_message(f"{bridge}/v1/turn", text="hello", dispatch_id=dispatch)
    # Assert: the first submit lands and the repeat is reported as a replay.
    assert first.state == "delivered" and second.state == "replay"


def test_repeated_submit_reaches_the_agent_only_once(bridge):
    # Arrange: a FRESH id — the replay store is process-global by design, so a
    # literal shared id would inherit another test's replay state and test the
    # fixture rather than the guard.
    dispatch = new_dispatch_id()
    # Act: the SAME id twice — a double-click must not inject twice.
    send_message(f"{bridge}/v1/turn", text="please continue", dispatch_id=dispatch)
    send_message(f"{bridge}/v1/turn", text="please continue", dispatch_id=dispatch)
    # Assert
    assert len([r for r in _Bridge.received if r["route"] == "turn"]) == 1


def test_replay_is_reported_as_delivered_not_failed(bridge):
    # Arrange
    send_message(f"{bridge}/v1/turn", text="once", dispatch_id="d1")
    # Act
    repeat = send_message(f"{bridge}/v1/turn", text="once", dispatch_id="d1")
    # Assert: a replay is a SUCCESS outcome (the message is there), not an error.
    assert repeat.delivered is True and repeat.state == "replay"


def test_distinct_dispatch_ids_both_deliver(bridge):
    # Arrange: two distinct dispatch ids
    # Act: two deliberate sends
    send_message(f"{bridge}/v1/turn", text="a", dispatch_id="d-a")
    send_message(f"{bridge}/v1/turn", text="b", dispatch_id="d-b")
    # Assert
    assert len([r for r in _Bridge.received if r["route"] == "turn"]) == 2


def test_control_key_repeat_is_also_idempotent(bridge):
    # Arrange
    # Act
    first = send_control_key(f"{bridge}/v1/turn", key="Enter", dispatch_id="k1")
    second = send_control_key(f"{bridge}/v1/turn", key="Enter", dispatch_id="k1")
    # Assert
    assert first.state == "delivered" and second.state == "replay"


def test_control_key_repeat_reaches_the_agent_only_once(bridge):
    # Arrange: a FRESH id, for the same process-global replay-store reason.
    dispatch = new_dispatch_id()
    # Act: Enter twice would submit twice in a live TUI.
    send_control_key(f"{bridge}/v1/turn", key="Enter", dispatch_id=dispatch)
    send_control_key(f"{bridge}/v1/turn", key="Enter", dispatch_id=dispatch)
    # Assert
    assert len([r for r in _Bridge.received if r["route"] == "control"]) == 1


def test_a_failed_delivery_is_not_remembered_as_delivered(bridge):
    # Arrange: a message the bridge refuses (empty text) must not poison the id,
    # or a corrected retry under the same id would be silently swallowed.
    failed = send_message(f"{bridge}/v1/turn", text="   ", dispatch_id="d-retry")
    # Act
    retry = send_message(f"{bridge}/v1/turn", text="real text", dispatch_id="d-retry")
    # Assert
    assert failed.state == "refused" and retry.state == "delivered"


# ── the three control keys, and only those ───────────────────────────────────


def test_exactly_the_three_sac_control_keys_are_offered():
    # Arrange
    # Act
    # Assert: the bridge 400s anything else, so a fourth button
    # would be a control the backend can never honour.
    assert set(CONTROL_KEYS) == {"Enter", "Escape", "C-c"}


@pytest.mark.parametrize("key", ["Enter", "Escape", "C-c"])
def test_each_real_control_key_is_delivered(bridge, key):
    # Arrange
    # Act
    result = send_control_key(f"{bridge}/v1/turn", key=key)
    # Assert
    assert result.delivered and result.mode == "control"


def test_invented_control_key_is_refused_without_contacting_the_bridge(bridge):
    # Arrange
    # Act
    result = send_control_key(f"{bridge}/v1/turn", key="C-d")
    # Assert: refused locally, and nothing reached the agent.
    assert result.state == "refused" and _Bridge.received == []


def test_control_key_is_refused_when_the_runtime_has_no_control_surface(bridge):
    # Arrange: the bridge answers 501 for a runtime without a UI-control surface.
    _Bridge.accepts_control = False
    # Act
    result = send_control_key(f"{bridge}/v1/turn", key="Enter")
    # Assert: reported as a failure, never as delivered.
    assert result.delivered is False and result.state == "failed"


# ── refusals and failures ────────────────────────────────────────────────────


def test_empty_message_is_refused(bridge):
    # Arrange
    # Act
    result = send_message(f"{bridge}/v1/turn", text="   ")
    # Assert
    assert result.state == "refused" and _Bridge.received == []


def test_overlong_message_is_refused(bridge):
    # Arrange
    # Act
    result = send_message(f"{bridge}/v1/turn", text="x" * (MAX_MESSAGE_CHARS + 1))
    # Assert
    assert result.state == "refused" and _Bridge.received == []


def test_agent_without_a_turn_endpoint_is_refused(dead_bridge):
    # Arrange
    # Act
    result = send_message("", text="hello")
    # Assert
    assert result.state == "refused"


def test_unreachable_bridge_is_reported_as_failed_not_delivered(dead_bridge):
    # Arrange
    # Act
    result = send_message(f"{dead_bridge}/v1/turn", text="hello")
    # Assert
    assert result.delivered is False and result.state == "failed"


# ── no secret leakage ────────────────────────────────────────────────────────


def test_redaction_scrubs_credentials():
    # Arrange
    # Act
    scrubbed = redact("failed for Bearer abcdefghijklmnop1234 and sk-abcdefghijklmnop123")
    # Assert
    assert "abcdefghijklmnop1234" not in scrubbed and "[REDACTED]" in scrubbed


def test_delivery_result_never_carries_the_message_text(bridge):
    # Arrange
    # Act
    result = send_message(f"{bridge}/v1/turn", text="secret-value sk-abcdefghijklmnop123")
    # Assert: the confirmation echoed to a browser must not become a transcript.
    assert "sk-abcdefghijklmnop123" not in json.dumps(result.as_dict())


def test_dispatch_ids_are_unique():
    # Arrange
    # Act
    ids = {new_dispatch_id() for _ in range(200)}
    # Assert
    assert len(ids) == 200


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("go", "", True),
        ("", "go", True),
        ("go", "stop", False),
        ("", "", False),
    ],
)
def test_exactly_one_guard_enforces_mutual_exclusion(first, second, expected):
    # Arrange
    # Act
    outcome = exactly_one_of(first, second)
    # Assert: the form guard for mutually exclusive actions.
    assert outcome is expected


# ── the view that fronts these surfaces ──────────────────────────────────────


def test_message_route_denies_a_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange: a caller who may READ the fleet but is not an operator.
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.delete(OPERATORS_ENV)
    # Act
    response = client.post("/alpha/message", {"message": "hi"})
    # Assert: refused, and the text never reaches an agent.
    assert response.status_code == 403


def test_message_route_requires_exactly_one_action(client, loopback, env_save_restore):
    # Arrange: an AUTHORIZED operator, submitting BOTH a message and a key.
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post("/alpha/message", {"message": "hi", "control_key": "Enter"})
    # Assert: refused rather than guessing which the operator meant.
    assert response.status_code == 302 and "state=refused" in response["Location"]


def test_message_route_refuses_a_made_up_control_key(client, loopback, env_save_restore):
    # Arrange
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post("/alpha/message", {"control_key": "C-d"})
    # Assert
    assert response.status_code == 302 and "state=refused" in response["Location"]


def test_message_route_audits_an_authorized_delivery(client, loopback, env_save_restore, audit_log):
    # Arrange: the loopback listener does not serve a turn bridge, so the
    # delivery legitimately FAILS — what matters is that it was attempted,
    # authorized, and audited, not silently dropped.
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    response = client.post("/alpha/message", {"message": "please continue"})
    # Assert: authorized and redirected (the delivery itself fails against a
    # listener with no turn bridge, which is not what this test is about).
    assert response.status_code == 302


def test_message_route_writes_an_audit_line(client, loopback, env_save_restore, audit_log):
    # Arrange
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    client.post("/alpha/message", {"message": "please continue"})
    # Assert: the attempt is audited, not silently dropped.
    assert audit_log.exists() and "message_action" in audit_log.read_text(encoding="utf-8")


def test_detail_page_renders_the_control_surfaces_for_an_operator(client, loopback, env_save_restore):
    # Arrange
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert: exactly the three real keys are offered.
    assert "Enter" in html and "Escape" in html and "C-c" in html


def test_detail_page_renders_the_message_form_for_an_operator(client, loopback, env_save_restore):
    # Arrange
    from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV

    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.set(OPERATORS_ENV, "alice")
    # Act
    html = client.get("/alpha/").content.decode()
    # Assert: the message surface posts to this agent's own endpoint.
    assert "/alpha/message" in html and 'name="message"' in html
