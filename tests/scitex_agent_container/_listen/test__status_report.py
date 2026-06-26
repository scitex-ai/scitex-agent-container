"""Tests for ``_listen/_status_report.py`` — ``sac listen status``
probe + render (card ``sac-listen-restart-selfheal-cli``).

No-mocks (PA-306): ``http_get`` / ``port_is_bound`` are plain
callables passed in, so a small recorded fake drives each state. No
MagicMock, no monkeypatch.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._listen._status_report import (
    build_status_payload,
    render_status_lines,
)


def _payload(
    *,
    http_status: int,
    bound: bool,
    pidfile_pid: int | None = 12345,
    pidfile_pid_alive: bool = True,
    pid_file: Path = Path("/run/listen-7878.pid"),
) -> dict:
    return build_status_payload(
        host="127.0.0.1",
        port=7878,
        pid_file=pid_file,
        pidfile_pid=pidfile_pid,
        pidfile_pid_alive=pidfile_pid_alive,
        health_path="/v1/health",
        http_get=lambda _url, _t: http_status,
        port_is_bound=lambda _h, _p: bound,
    )


# ---------------------------------------------------------------------------
# build_status_payload — liveness classification
# ---------------------------------------------------------------------------


def test_running_true_on_http_200() -> None:
    # Arrange
    # Act
    payload = _payload(http_status=200, bound=True)
    # Assert
    assert payload["running"] is True


def test_running_true_on_http_401_under_bearer_auth() -> None:
    # Arrange — a 401 proves the daemon answered (auth-change-proof).
    # Act
    payload = _payload(http_status=401, bound=True)
    # Assert
    assert payload["running"] is True


def test_running_false_on_transport_failure() -> None:
    # Arrange — -1 means connection refused / timeout → down.
    # Act
    payload = _payload(http_status=-1, bound=False)
    # Assert
    assert payload["running"] is False


def test_payload_probes_v1_health_url() -> None:
    # Arrange
    # Act
    payload = _payload(http_status=200, bound=True)
    # Assert
    assert payload["health_url"] == "http://127.0.0.1:7878/v1/health"


def test_payload_reports_bind_address() -> None:
    # Arrange
    # Act
    payload = _payload(http_status=200, bound=True)
    # Assert
    assert payload["bind"] == "127.0.0.1:7878"


# ---------------------------------------------------------------------------
# render_status_lines — UP / WEDGED / DOWN headline
# ---------------------------------------------------------------------------


def test_render_headline_up_when_serving() -> None:
    # Arrange
    payload = _payload(http_status=200, bound=True)
    # Act
    lines = render_status_lines(payload)
    # Assert
    assert "UP (serving)" in lines[0]


def test_render_headline_wedged_when_bound_but_not_serving() -> None:
    # Arrange — port bound but health does not answer (the wedged case).
    payload = _payload(http_status=-1, bound=True)
    # Act
    lines = render_status_lines(payload)
    # Assert
    assert "WEDGED" in lines[0]


def test_render_headline_down_when_not_bound() -> None:
    # Arrange
    payload = _payload(http_status=-1, bound=False)
    # Act
    lines = render_status_lines(payload)
    # Assert
    assert "DOWN" in lines[0]


def test_render_marks_dead_pidfile_pid() -> None:
    # Arrange — pidfile names a PID that is no longer alive (stale).
    payload = _payload(http_status=-1, bound=False, pidfile_pid_alive=False)
    # Act
    text = "\n".join(render_status_lines(payload))
    # Assert
    assert "DEAD (stale pidfile)" in text


def test_render_emits_restart_hint_when_down() -> None:
    # Arrange
    payload = _payload(http_status=-1, bound=False)
    # Act
    text = "\n".join(render_status_lines(payload))
    # Assert
    assert "sac listen restart" in text


def test_render_no_hint_when_serving() -> None:
    # Arrange
    payload = _payload(http_status=200, bound=True)
    # Act
    text = "\n".join(render_status_lines(payload))
    # Assert
    assert "hint:" not in text
