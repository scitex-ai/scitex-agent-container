"""Receiver-side ``kind`` propagation in ``message:send`` (ADR-0013 Phase 1).

The receiver pulls ``params.metadata.kind`` and lands it on the
:func:`mint_event` envelope so subscribers of the lead's inbox can
filter typed agent push events (``done`` / ``blocker`` / ``status``).
Non-string ``kind`` is a loud 400 — silently coercing would let bad
senders poison the inbox shape.

No mocks (PA-306): drives the real Starlette app through the real
``TestClient``. One assertion per test (PA-307).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import record_lineage

TOKEN = "test-kind-pin-token"


@pytest.fixture
def kind_env(tmp_path: Path):
    """Isolated state.db so ``channel_events`` reads back our send."""
    saved_home = os.environ.get("HOME")
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_db_const = state_db.DEFAULT_DB_PATH

    db = tmp_path / "state.db"
    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)

    # Same-group ACL — alice (sender) and lead (target) both rooted.
    record_lineage(child="alice", parent="root", db_path=db)
    record_lineage(child="lead", parent="root", db_path=db)

    try:
        yield {"db": db, "tmp_path": tmp_path}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for k, v in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db_env),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _send(client: TestClient, *, kind, headers) -> object:
    """POST a message:send envelope to ``lead`` with the given ``kind``."""
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "ping"}],
            },
            "metadata": {"from_agent": "alice", "kind": kind},
        },
    }
    return client.post("/agents/lead/message:send", json=body, headers=headers)


def test_string_kind_lands_on_persisted_event(kind_env) -> None:
    # Arrange — real app, real state.db; same-group ACL was set up.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act
    with TestClient(app) as client:
        _send(client, kind="done", headers=headers)
    rows = list_undelivered(target="lead")
    # Assert
    assert rows and rows[0]["event"].get("kind") == "done"


def test_string_kind_round_trips_status(kind_env) -> None:
    # Arrange
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act
    with TestClient(app) as client:
        _send(client, kind="status", headers=headers)
    rows = list_undelivered(target="lead")
    # Assert
    assert rows and rows[0]["event"].get("kind") == "status"


def test_missing_kind_yields_no_kind_field(kind_env) -> None:
    # Arrange — the receiver only stamps ``kind`` when the sender sends
    # one; absence must stay absent so legacy callers (auto-ack, dispatch
    # ledger, ...) are unchanged.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "ping"}],
            },
            "metadata": {"from_agent": "alice"},
        },
    }
    # Act
    with TestClient(app) as client:
        client.post("/agents/lead/message:send", json=body, headers=headers)
    rows = list_undelivered(target="lead")
    # Assert
    assert rows and "kind" not in rows[0]["event"]


def test_non_string_kind_is_loud_400(kind_env) -> None:
    # Arrange — a numeric ``kind`` would silently round-trip as JSON
    # if the receiver coerced; refuse it loudly so bad senders cannot
    # poison the inbox shape.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "ping"}],
            },
            "metadata": {"from_agent": "alice", "kind": 7},
        },
    }
    # Act
    with TestClient(app) as client:
        resp = client.post("/agents/lead/message:send", json=body, headers=headers)
    # Assert
    assert resp.status_code == 400


def test_non_string_kind_400_names_metadata_field(kind_env) -> None:
    # Arrange — the 400 body must say where the bad value came from
    # so an operator chasing a failed push sees what to fix.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": "ping"}],
            },
            "metadata": {"from_agent": "alice", "kind": ["done"]},
        },
    }
    # Act
    with TestClient(app) as client:
        resp = client.post("/agents/lead/message:send", json=body, headers=headers)
    err = resp.json().get("error", "")
    # Assert
    assert "metadata.kind" in err
