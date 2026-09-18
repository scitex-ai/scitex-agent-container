"""Tests for ``_django/_remote`` (the authenticated SAC control-plane client).

Exercises the real client against a REAL loopback HTTP listener serving the
contract (the ``loopback`` fixture) — no mocks, no monkeypatch. Each test has
AAA markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

from scitex_agent_container._django._remote import (
    FleetUnavailableError,
    RemoteFleet,
    RemoteOperationError,
    resolve_token,
)

from .conftest import TOKEN


def test_resolve_token_prefers_env(env_save_restore):
    # Arrange
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", "env-token")
    # Act
    token = resolve_token("http://127.0.0.1:1")
    # Assert
    assert token == "env-token"


def test_resolve_token_falls_back_to_token_file(env_save_restore, tmp_path):
    # Arrange
    path = tmp_path / "tok"
    path.write_text("file-token\n", encoding="utf-8")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN_FILE", str(path))
    # Act
    token = resolve_token("http://127.0.0.1:1")
    # Assert
    assert token == "file-token"


def test_list_all_returns_scoped_rows(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    rows = fleet.list_all()
    # Assert
    assert {r["name"] for r in rows} == {"alpha", "beta", "gamma", "delta"}


def test_read_status_returns_body(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    status = fleet.read_status("alpha")
    # Assert
    assert status["liveness"]["verdict"] == "ALIVE"


def test_read_status_typed_error_carries_kind(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    try:
        fleet.read_status("delta")
        raised = False
    except RemoteOperationError as exc:
        raised = exc.kind == "spec_resolution_failed" and exc.status_code == 400
    # Assert
    assert raised


def test_read_statuses_uses_one_batched_agents_read(loopback, listener_requests):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    results = fleet.read_statuses(["alpha", "delta"])
    # Assert
    assert set(results) == {"alpha", "delta"} and listener_requests == ["/agents"]


def test_read_tail_returns_sse_body(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    tail = fleet.read_tail("alpha")
    # Assert
    assert "start the job" in tail and "sk-abc123DEF456GHI789jkl012" in tail


def test_read_tail_404_is_empty(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", TOKEN)
    # Act
    tail = fleet.read_tail("beta")
    # Assert
    assert tail == ""


def test_unreachable_listener_raises_fleet_unavailable():
    # Arrange
    fleet = RemoteFleet("http://127.0.0.1:1", TOKEN)
    # Act
    try:
        fleet.list_all()
        raised = False
    except FleetUnavailableError:
        raised = True
    # Assert
    assert raised


def test_wrong_token_is_rejected(loopback):
    # Arrange
    fleet = RemoteFleet(f"http://127.0.0.1:{loopback}", "not-the-token")
    # Act
    try:
        fleet.list_all()
        raised = False
    except RemoteOperationError as exc:
        raised = exc.status_code == 401
    # Assert
    assert raised
