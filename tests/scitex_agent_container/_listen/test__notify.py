"""Unit tests for the interim ``POST /v1/notify`` delivery endpoint.

Mirrors ``src/scitex_agent_container/_listen/_notify.py`` (PS-204 §2).

What this proves
================
``/v1/notify`` is the interim unblock for the scitex-todo escalation:
the board POSTs here INSTEAD of a containerized agent's unreachable
``turn_url``, and the body is published into the agent's a2a inbox bus
via the SAME :class:`~scitex_agent_container.a2a._inbox_bus.Broker`
publish path ``a2a_send`` uses — so a subscribed (containerized) agent
receives it.

No mocks (STX-NM002)
====================
* The bus half (:func:`publish_to_agent`) is exercised against a REAL
  in-process :class:`Broker` with a REAL subscriber: we ``subscribe`` a
  queue, publish, and assert the exact event lands on that queue. Nothing
  is mocked.
* The HTTP half is driven through the REAL Starlette app via the REAL
  ``TestClient`` (a real ASGI round-trip), against a REAL isolated
  state.db — bearer gating, body validation, and the persisted
  ``channel_events`` row are all asserted against real behaviour.

TQ: AAA markers (TQ002); 3+-word names; the state.db fixture is FUNCTION
scoped (TQ004) and ``yield``s (TQ005).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._notify import publish_to_agent
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container.a2a._inbox_bus import Broker

TOKEN = "test-notify-token"


@pytest.fixture
def notify_env(tmp_path: Path, pg_schema: str):
    """Isolated state.db + registry + PostgreSQL schema for ``/v1/notify``.

    ``pg_schema`` joined this fixture on 2026-08-28: ``channel_events`` left
    SQLite for the shared PostgreSQL (ADR-0023), so ``publish_to_agent``'s
    persist half now needs a REAL throwaway schema. The isolated ``state.db``
    stays for everything else the listen app touches.

    Function-scoped (TQ004 — no session/module-scoped state mutation) and
    ``yield``s after acquiring its resources (TQ005). Restores every env
    var + module constant it overrides.
    """
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


# ---------------------------------------------------------------------------
# Bus half: publish_to_agent against a REAL broker + REAL subscriber.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_reaches_a_real_subscriber(notify_env) -> None:
    # Arrange — a REAL broker with a REAL subscriber queue for the agent.
    broker = Broker()
    queue = await broker.subscribe("worker-x")
    # Act — publish through the production helper (persist + publish).
    await publish_to_agent(broker, agent="worker-x", body="card 42 commented")
    # Assert — the subscriber received the event body verbatim.
    event = queue.get_nowait()
    assert event["content"] == "card 42 commented"


@pytest.mark.asyncio
async def test_publish_reports_delivered_subscriber_count(notify_env) -> None:
    # Arrange — two REAL subscribers on the same agent's bus.
    broker = Broker()
    await broker.subscribe("worker-x")
    await broker.subscribe("worker-x")
    # Act
    result = await publish_to_agent(broker, agent="worker-x", body="ping")
    # Assert — both live subscribers are counted.
    assert result["delivered_subscriber_count"] == 2


@pytest.mark.asyncio
async def test_publish_threads_card_id_into_extra(notify_env) -> None:
    # Arrange
    broker = Broker()
    queue = await broker.subscribe("worker-x")
    # Act
    await publish_to_agent(
        broker, agent="worker-x", body="see card", card_id="card-99"
    )
    # Assert — card_id rides under ``extra`` for card-aware consumers.
    event = queue.get_nowait()
    assert (event.get("extra") or {}).get("card_id") == "card-99"


# ---------------------------------------------------------------------------
# HTTP half: /v1/notify through the real app over a real ASGI round-trip.
# ---------------------------------------------------------------------------


def test_notify_persists_event_for_replay(notify_env) -> None:
    # Arrange — real app + real state.db; no subscriber connected yet, so
    # durability (persist-before-publish) is what carries the event.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act
    with TestClient(app) as client:
        resp = client.post(
            "/v1/notify",
            json={"agent": "worker-y", "body": "card 7 reassigned"},
            headers=headers,
        )
    rows = list_undelivered(target="worker-y")
    # Assert — 200 and the body is durably queued for the next connect.
    assert resp.status_code == 200 and rows[0]["event"]["content"] == (
        "card 7 reassigned"
    )


def test_notify_requires_bearer_token(notify_env) -> None:
    # Arrange — same control-plane bearer gate as the rest of 7878.
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    # Act — no Authorization header.
    with TestClient(app) as client:
        resp = client.post(
            "/v1/notify", json={"agent": "worker-y", "body": "hi"}
        )
    # Assert
    assert resp.status_code == 401


def test_notify_rejects_missing_agent_loudly(notify_env) -> None:
    # Arrange
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act — body present, agent missing.
    with TestClient(app) as client:
        resp = client.post("/v1/notify", json={"body": "hi"}, headers=headers)
    # Assert — fail loud (400), not a silent accept.
    assert resp.status_code == 400


def test_notify_rejects_empty_body_loudly(notify_env) -> None:
    # Arrange
    app = create_app(token=TOKEN, local_host="127.0.0.1")
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act — agent present, body blank.
    with TestClient(app) as client:
        resp = client.post(
            "/v1/notify",
            json={"agent": "worker-y", "body": "   "},
            headers=headers,
        )
    # Assert
    assert resp.status_code == 400
