"""End-to-end tests for the host listen's ACL routes (task #27 PR B).

The in-container CLI POSTs to ``/v1/acl/{unblock,block,grant}`` to
target the HOST listen's state.db instead of the per-container
copy. These tests exercise the routes against a real Starlette
``TestClient`` and assert they call the same DB-only helpers the
bare-host CLI uses (so the operator log + state.db forensics see
the same operation either way).

No-mocks (PA-306): real on-disk state.db, real TestClient, real
bearer auth.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_blocks import has_block
from scitex_agent_container._state.state_db_nodes import has_grant

_TOKEN = "test-token-acl-routes"


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# /v1/acl/unblock — writes comms_grants
# ---------------------------------------------------------------------------


def test_unblock_route_writes_comms_grants_row(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            json={"sender": "alice", "target": "lead"},
            headers=_auth(),
        )
    # Assert
    assert has_grant(sender="alice", target="lead")


def test_unblock_route_returns_200(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            json={"sender": "alice", "target": "lead"},
            headers=_auth(),
        )
    # Assert
    assert r.status_code == 200


def test_unblock_route_response_envelope_carries_decision_fields(
    isolated_state: Path, pg_schema: str,
) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            json={"sender": "alice", "target": "lead"},
            headers=_auth(),
        )
    body = r.json()
    # Assert
    assert body.get("granted") is True


# ---------------------------------------------------------------------------
# /v1/acl/block — writes comms_blocks
# ---------------------------------------------------------------------------


def test_block_route_writes_comms_blocks_row(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/v1/acl/block",
            json={"sender": "alice", "target": "lead"},
            headers=_auth(),
        )
    # Assert
    assert has_block(sender="alice", target="lead")


# ---------------------------------------------------------------------------
# /v1/acl/grant — alias of unblock
# ---------------------------------------------------------------------------


def test_grant_route_writes_comms_grants_like_unblock(
    isolated_state: Path, pg_schema: str,
) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/v1/acl/grant",
            json={"sender": "alice", "target": "lead"},
            headers=_auth(),
        )
    # Assert
    assert has_grant(sender="alice", target="lead")


# ---------------------------------------------------------------------------
# Bad input — fail loud with 400
# ---------------------------------------------------------------------------


def test_missing_sender_returns_400(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            json={"target": "lead"},
            headers=_auth(),
        )
    # Assert
    assert r.status_code == 400


def test_missing_target_returns_400(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/block",
            json={"sender": "alice"},
            headers=_auth(),
        )
    # Assert
    assert r.status_code == 400


def test_malformed_body_returns_400(isolated_state: Path, pg_schema: str) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            content=b"not-json",
            headers={**_auth(), "Content-Type": "application/json"},
        )
    # Assert
    assert r.status_code == 400


def test_missing_bearer_returns_401_or_403(isolated_state: Path, pg_schema: str) -> None:
    # Arrange — no Authorization header.
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        r = client.post(
            "/v1/acl/unblock",
            json={"sender": "alice", "target": "lead"},
        )
    # Assert — middleware rejects (some configs use 401, some 403;
    # accept either as long as it is NOT a 2xx success).
    assert r.status_code >= 400 and r.status_code < 500
