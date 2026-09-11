from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container.a2a import _inbox_ack as ack


def test_ack_marks_only_the_addressed_target_after_acceptance(monkeypatch):
    calls = []

    async def fake_run_blocking(fn, row_ids, *, target):
        calls.append((fn, row_ids, target))

    monkeypatch.setattr(ack, "run_blocking", fake_run_blocking)
    app = Starlette(
        routes=[
            Route(
                "/agents/{name}/inbox/ack",
                ack.inbox_ack,
                methods=["POST"],
            )
        ]
    )

    response = TestClient(app).post("/agents/scholar/inbox/ack", json={"id": 41})

    assert response.status_code == 200
    assert response.json() == {"acknowledged": 41}
    assert calls == [(ack.mark_delivered, [41], "scholar")]


def test_ack_rejects_invalid_or_unknown_rows_without_marking(monkeypatch):
    calls = []

    async def fake_run_blocking(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(ack, "run_blocking", fake_run_blocking)

    async def endpoint(request):
        return await ack.inbox_ack(request, known_names={"scholar"})

    app = Starlette(
        routes=[Route("/agents/{name}/inbox/ack", endpoint, methods=["POST"])]
    )
    client = TestClient(app)

    assert client.post("/agents/writer/inbox/ack", json={"id": 1}).status_code == 404
    assert client.post("/agents/scholar/inbox/ack", json={"id": 0}).status_code == 400
    assert calls == []
