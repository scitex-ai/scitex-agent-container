"""Tests for the ``a2a_proxy`` runner — forward-only A2A bridge.

Uses Starlette's TestClient against ``build_app`` with an in-process
upstream stub mounted on httpx's ASGITransport. No live network; no
``respx`` dependency.
"""

from __future__ import annotations

from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container._runners.a2a_proxy import build_app, splice_card

# ---------------------------------------------------------------------------
# Upstream stubs (in-process ASGI app, accessed via httpx.AsyncClient)
# ---------------------------------------------------------------------------


def _make_upstream(
    *,
    turn_handler: Any = None,
    card: dict[str, Any] | None = None,
) -> Starlette:
    async def post_turn(request: Request) -> JSONResponse:
        if turn_handler is None:
            body = await request.json()
            return JSONResponse({"reply": f"echo:{body.get('text', '')}"})
        return await turn_handler(request)

    async def get_card(request: Request) -> JSONResponse:
        return JSONResponse(card or {"name": "upstream", "skills": []})

    return Starlette(
        routes=[
            Route("/v1/turn", post_turn, methods=["POST"]),
            Route("/.well-known/agent-card.json", get_card, methods=["GET"]),
        ]
    )


def _client_for_upstream(upstream_app: Starlette) -> httpx.AsyncClient:
    """Return an AsyncClient that routes requests into the in-process app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )


# ---------------------------------------------------------------------------
# splice_card — pure logic
# ---------------------------------------------------------------------------


def test_splice_card_preserves_upstream_skills_overrides_name_and_url() -> None:
    upstream = {
        "name": "real-peer",
        "url": "http://peer/agents/real-peer",
        "skills": [{"id": "peer.do-things", "name": "do-things"}],
        "capabilities": {"streaming": True},
        "provider": {"organization": "peer-org"},
    }
    out = splice_card(
        upstream,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
    )
    assert out["name"] == "proxy-front"
    assert out["url"] == "http://us/agents/proxy-front"
    assert out["skills"] == upstream["skills"]
    assert out["capabilities"] == upstream["capabilities"]
    assert out["provider"] == upstream["provider"]
    assert out["x-scitex-agent-container"] == {
        "kind": "AgentProxy",
        "upstream": "https://peer.example.com",
        "trust": "local-mesh",
    }


def test_splice_card_surfaces_fetch_error_when_upstream_none() -> None:
    out = splice_card(
        None,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="untrusted",
        fetch_error="ConnectError: dial timeout",
    )
    assert out["name"] == "proxy-front"
    assert out["x-scitex-agent-container"]["upstream_card_fetch_error"] == (
        "ConnectError: dial timeout"
    )


# ---------------------------------------------------------------------------
# POST /v1/turn — forward roundtrip
# ---------------------------------------------------------------------------


def test_post_v1_turn_forwards_to_upstream_and_returns_reply() -> None:
    upstream = _make_upstream()
    client = _client_for_upstream(upstream)
    app = build_app(
        name="proxy-front",
        upstream="http://upstream.test",
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        httpx_client=client,
    )
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "hello"})
        assert r.status_code == 200
        assert r.json() == {"reply": "echo:hello"}


# ---------------------------------------------------------------------------
# Timeout → 504
# ---------------------------------------------------------------------------


def test_upstream_timeout_returns_504() -> None:
    async def hang(request: Request) -> Response:
        raise httpx.ReadTimeout("upstream hung")

    upstream = _make_upstream(turn_handler=hang)
    client = _client_for_upstream(upstream)
    app = build_app(
        name="proxy-front",
        upstream="http://upstream.test",
        trust="untrusted",
        redact=[],
        timeout_s=0.5,
        upstream_card=None,
        httpx_client=client,
    )
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "hi"})
        assert r.status_code == 504
        assert "timeout" in r.json()["error"].lower()


# ---------------------------------------------------------------------------
# Upstream 5xx → our 502
# ---------------------------------------------------------------------------


def test_upstream_500_returns_502_with_message() -> None:
    async def boom(request: Request) -> Response:
        return Response("kaboom", status_code=500)

    upstream = _make_upstream(turn_handler=boom)
    client = _client_for_upstream(upstream)
    app = build_app(
        name="proxy-front",
        upstream="http://upstream.test",
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        httpx_client=client,
    )
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "hi"})
        assert r.status_code == 502
        assert "kaboom" in r.json()["error"]


# ---------------------------------------------------------------------------
# Redaction → 400 before forwarding
# ---------------------------------------------------------------------------


def test_redact_term_in_prompt_returns_400() -> None:
    called = {"forwarded": False}

    async def handler(request: Request) -> Response:
        called["forwarded"] = True
        return JSONResponse({"reply": "should-not-see-this"})

    upstream = _make_upstream(turn_handler=handler)
    client = _client_for_upstream(upstream)
    app = build_app(
        name="proxy-front",
        upstream="http://upstream.test",
        trust="untrusted",
        redact=["SECRET"],
        timeout_s=5.0,
        upstream_card=None,
        httpx_client=client,
    )
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "leaking SECRET data"})
        assert r.status_code == 400
        assert "redacted" in r.json()["error"]
    assert called["forwarded"] is False


# ---------------------------------------------------------------------------
# Redirect to disallowed host → 502
# ---------------------------------------------------------------------------


def test_upstream_redirect_to_other_host_returns_502() -> None:
    async def redirector(request: Request) -> Response:
        return Response(
            "",
            status_code=302,
            headers={"location": "http://evil.example.com/v1/turn"},
        )

    upstream = _make_upstream(turn_handler=redirector)
    client = _client_for_upstream(upstream)
    app = build_app(
        name="proxy-front",
        upstream="http://upstream.test",
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        httpx_client=client,
    )
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "hi"})
        assert r.status_code == 502
        assert "disallowed host" in r.json()["error"]
        assert "evil.example.com" in r.json()["error"]


# ---------------------------------------------------------------------------
# GET /.well-known/agent-card.json — card splicing in-flight
# ---------------------------------------------------------------------------


def test_agent_card_splices_upstream_card_with_our_overrides() -> None:
    upstream_card = {
        "name": "real-peer",
        "url": "http://peer/agents/real-peer",
        "skills": [{"id": "peer.do-things", "name": "do-things"}],
        "capabilities": {"streaming": True},
    }
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=[],
        timeout_s=5.0,
        upstream_card=upstream_card,
    )
    with TestClient(app) as tc:
        r = tc.get("/.well-known/agent-card.json")
        assert r.status_code == 200
        card = r.json()
        assert card["name"] == "proxy-front"
        assert card["skills"] == upstream_card["skills"]
        assert card["capabilities"] == upstream_card["capabilities"]
        assert card["x-scitex-agent-container"]["kind"] == "AgentProxy"
        assert (
            card["x-scitex-agent-container"]["upstream"] == "https://peer.example.com"
        )
        assert card["x-scitex-agent-container"]["trust"] == "local-mesh"
        # url is request-derived (testserver host); just sanity check shape.
        assert "/agents/proxy-front" in card["url"]


def test_agent_card_fallback_when_upstream_card_missing() -> None:
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        upstream_card_error="ConnectError: dial timeout",
    )
    with TestClient(app) as tc:
        r = tc.get("/.well-known/agent.json")  # mirror route
        assert r.status_code == 200
        card = r.json()
        assert card["name"] == "proxy-front"
        assert (
            card["x-scitex-agent-container"]["upstream_card_fetch_error"]
            == "ConnectError: dial timeout"
        )


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_returns_upstream_and_trust() -> None:
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
    )
    with TestClient(app) as tc:
        r = tc.get("/health")
        assert r.status_code == 200
        assert r.json() == {
            "status": "ok",
            "upstream": "https://peer.example.com",
            "trust": "local-mesh",
        }
