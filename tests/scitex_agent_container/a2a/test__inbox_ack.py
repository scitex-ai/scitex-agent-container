from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container.a2a import _inbox_ack as ack


def test_ack_marks_only_the_addressed_target_after_acceptance():
    # Arrange
    calls = []

    async def fake_run_blocking(fn, row_ids, *, target):
        calls.append((fn, row_ids, target))

    async def endpoint(request):
        return await ack.inbox_ack(request, run=fake_run_blocking)

    app = Starlette(
        routes=[
            Route(
                "/agents/{name}/inbox/ack",
                endpoint,
                methods=["POST"],
            )
        ]
    )

    # Act
    response = TestClient(app).post("/agents/scholar/inbox/ack", json={"id": 41})
    # Assert
    assert (response.status_code, response.json(), calls) == (
        200,
        {"acknowledged": 41},
        [(ack.mark_delivered, [41], "scholar")],
    )


def test_ack_rejects_invalid_or_unknown_rows_without_marking():
    # Arrange
    calls = []

    async def fake_run_blocking(*args, **kwargs):
        calls.append((args, kwargs))

    async def endpoint(request):
        return await ack.inbox_ack(
            request, known_names={"scholar"}, run=fake_run_blocking
        )

    app = Starlette(
        routes=[Route("/agents/{name}/inbox/ack", endpoint, methods=["POST"])]
    )
    client = TestClient(app)
    # Act
    outcomes = (
        client.post("/agents/writer/inbox/ack", json={"id": 1}).status_code,
        client.post("/agents/scholar/inbox/ack", json={"id": 0}).status_code,
        calls,
    )
    # Assert
    assert outcomes == (404, 400, [])
