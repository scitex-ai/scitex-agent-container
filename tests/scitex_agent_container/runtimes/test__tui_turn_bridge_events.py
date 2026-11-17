"""TUI turn bridge (``runtimes._tui_turn_bridge``): bridge lifecycle events, turn-text extraction, endpoint-gone.

Real seams only (PA-306 no-mocks); see test__tui_turn_bridge.py.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _PID,
    _PORT,
)

# ---------------------------------------------------------------------------
# write_bridge_event — the lifecycle log (tui-turn-bridge.log was 0 bytes)
# ---------------------------------------------------------------------------
# Measured on the host 2026-08-11: 16 of 17 ``tui-turn-bridge.log`` files were
# EMPTY. The launcher opens the file and hands it to the child as stdout+stderr,
# but the bridge wrote nothing of its own, so the log only ever captured an
# unhandled traceback — and when 14 bridges were found dead, not one death
# could be explained. These tests pin the two lines that bracket a bridge's
# life. Real files, real fds; no mocks.


def test_write_bridge_event_records_the_event_name(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        line = bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert " bind " in line


def test_write_bridge_event_records_the_bound_port(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"port={_PORT}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_records_the_pid(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"pid={_PID}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_records_the_host(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"host={DEFAULT_A2A_HOST}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_flushes_so_a_crash_cannot_swallow_the_line(
    tmp_path: Path,
) -> None:
    # Arrange — read the file through a SEPARATE handle while the writer is
    # still open: only a real flush makes the bytes visible, which is what
    # keeps the bind line readable after an abrupt death.
    log_path = tmp_path / "tui-turn-bridge.log"
    observed = ""
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
        observed = log_path.read_text(encoding="utf-8")
    # Assert
    assert "tui-turn-bridge bind" in observed


def test_write_bridge_event_appends_two_lines_for_bind_then_shutdown(
    tmp_path: Path,
) -> None:
    # Arrange — the shutdown line must BRACKET the bind line in the same log.
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
        bridge.write_bridge_event(
            fh,
            "shutdown",
            agent="figrecipe",
            host=DEFAULT_A2A_HOST,
            port=_PORT,
            pid=_PID,
        )
    # Assert
    assert log_path.read_text(encoding="utf-8").count("\n") == 2


def test_write_bridge_event_names_the_agent(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh,
            "shutdown",
            agent="figrecipe",
            host=DEFAULT_A2A_HOST,
            port=_PORT,
            pid=_PID,
        )
    # Assert
    assert "agent=figrecipe" in log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# extract_turn_text — the two body shapes that actually reach this bridge
# ---------------------------------------------------------------------------
def test_extract_turn_text_reads_the_flat_body() -> None:
    """The shape `sac listen` synthesises for a local wake."""
    # Arrange
    body = {"text": "hello", "from_agent": "peer"}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "hello"


def test_extract_turn_text_reads_the_a2a_envelope() -> None:
    """The shape every a2a caller in this package sends.

    Regression for 2026-09-02: after the message:send route alias landed, a
    real peer message still bounced with `missing or empty text field`,
    because the bridge read only body["text"] while _wrap_message_send nests
    the content at params.message.parts[].text.
    """
    # Arrange
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {"message": {"parts": [{"text": "from a peer"}]}, "metadata": {}},
    }
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "from a peer"


def test_extract_turn_text_returns_envelope_metadata_for_from_agent() -> None:
    """sac extension fields live under params.metadata, not at the root.

    A2A v1 rejects unknown fields at the params root, so the sender cannot put
    them where the flat reader looked; without this the inbound would be
    recorded with no requester and the completion report would be owed to
    nobody.
    """
    # Arrange
    body = {
        "params": {
            "message": {"parts": [{"text": "x"}]},
            "metadata": {"from_agent": "business", "dispatch_id": "d1"},
        }
    }
    # Act
    _text, meta = bridge.extract_turn_text(body)
    # Assert
    assert meta["from_agent"] == "business"


def test_extract_turn_text_joins_multipart_instead_of_dropping_the_tail() -> None:
    """Taking parts[0] would silently truncate a multi-part message."""
    # Arrange
    body = {"params": {"message": {"parts": [{"text": "a"}, {"text": "b"}]}}}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "a\nb"


def test_extract_turn_text_rejects_an_empty_envelope() -> None:
    """An envelope with no usable text must still be refused, not injected."""
    # Arrange
    body = {"params": {"message": {"parts": [{"text": "   "}]}}}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text is None


def test_tui_endpoint_gone_fires_on_connection_errno() -> None:
    """A wedged-live TUI must never be restarted; only a gone endpoint kicks."""
    # Arrange
    from scitex_dev.status import StatusCode

    gone = StatusCode(kind="errno", code="ECONNREFUSED", message="refused")
    # Act
    fires = bridge._tui_endpoint_gone(gone)
    # Assert
    assert fires is True


def test_tui_endpoint_gone_ignores_a_visibility_failure() -> None:
    """An HTTP delivery failure is a visibility problem, not a gone endpoint."""
    # Arrange
    from scitex_dev.status import StatusCode

    visibility = StatusCode(kind="http", code=502, message="delivery failed")
    # Act
    silent = bridge._tui_endpoint_gone(visibility)
    # Assert
    assert silent is False


def test_tui_endpoint_gone_ignores_a_missing_status() -> None:
    """No status at all must never read as a gone endpoint."""
    # Arrange
    missing = None
    # Act
    silent = bridge._tui_endpoint_gone(missing)
    # Assert
    assert silent is False
