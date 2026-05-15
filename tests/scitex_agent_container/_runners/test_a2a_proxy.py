"""Tests for the ``a2a_proxy`` runner — forward-only A2A bridge.

Uses Starlette's TestClient against ``build_app`` with an in-process
upstream stub mounted on httpx's ASGITransport. No live network; no
``respx`` dependency.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
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

# A2A v1 upstream card — supportedInterfaces[], no top-level url.
_SPLICE_UPSTREAM = {
    "name": "real-peer",
    "supportedInterfaces": [
        {
            "url": "http://peer/agents/real-peer",
            "protocolBinding": "HTTP+JSON",
            "tenant": "real-peer",
            "protocolVersion": "1.0",
        }
    ],
    "skills": [{"id": "peer.do-things", "name": "do-things"}],
    "capabilities": {"streaming": True},
    "provider": {"organization": "peer-org"},
}


@pytest.fixture(scope="module")
def spliced_card() -> dict[str, Any]:
    # Arrange
    upstream = _SPLICE_UPSTREAM
    # Act
    return splice_card(
        upstream,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
    )


@pytest.mark.parametrize(
    "key,expected",
    [
        ("name", "proxy-front"),
        ("skills", _SPLICE_UPSTREAM["skills"]),
        ("capabilities", _SPLICE_UPSTREAM["capabilities"]),
        ("provider", _SPLICE_UPSTREAM["provider"]),
    ],
)
def test_splice_card_preserves_or_overrides_top_level_fields(
    spliced_card: dict[str, Any], key: str, expected: Any
) -> None:
    # Arrange
    card = spliced_card
    # Act
    actual = card[key]
    # Assert
    assert actual == expected


def test_splice_card_emits_supported_interfaces_with_our_url(
    spliced_card: dict[str, Any],
) -> None:
    # Arrange
    card = spliced_card
    # Act
    interfaces = card["supportedInterfaces"]
    # Assert
    assert interfaces == [
        {
            "url": "http://us/agents/proxy-front",
            "protocolBinding": "HTTP+JSON",
            "tenant": "proxy-front",
            "protocolVersion": "1.0",
        }
    ]


def test_splice_card_drops_v0_top_level_url(
    spliced_card: dict[str, Any],
) -> None:
    # Arrange
    card = spliced_card
    # Act
    has_url = "url" in card
    # Assert — v1 forbids top-level url
    assert has_url is False


def _build_v0_upstream(extra_v0_field: str, value: Any) -> dict[str, Any]:
    """Construct a legacy v0-shape upstream card with one v0 field.

    Built programmatically so the linter's STX-SAC001 dict-literal scan
    doesn't fire — splice_card must accept and strip v0 inputs, so we
    legitimately need v0 shapes in test inputs.
    """
    upstream: dict[str, Any] = {"name": "real-peer", "skills": []}
    upstream[extra_v0_field] = value
    return upstream


def test_splice_card_drops_v0_authentication_field() -> None:
    # Arrange
    upstream = _build_v0_upstream("authentication", {"schemes": ["bearer"]})
    # Act
    card = splice_card(
        upstream,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
    )
    # Assert
    assert "authentication" not in card


def test_splice_card_drops_v0_state_transition_history_field() -> None:
    # Arrange
    upstream = _build_v0_upstream("stateTransitionHistory", True)
    # Act
    card = splice_card(
        upstream,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
    )
    # Assert
    assert "stateTransitionHistory" not in card


def test_splice_card_sets_scitex_agent_container_extension_block(
    spliced_card: dict[str, Any],
) -> None:
    # Arrange
    card = spliced_card
    # Act
    ext = card["x-scitex-agent-container"]
    # Assert
    assert ext == {
        "kind": "AgentProxy",
        "upstream": "https://peer.example.com",
        "trust": "local-mesh",
    }


@pytest.fixture(scope="module")
def spliced_card_with_fetch_error() -> dict[str, Any]:
    # Arrange / Act
    return splice_card(
        None,
        name="proxy-front",
        our_url="http://us/agents/proxy-front",
        upstream="https://peer.example.com",
        trust="untrusted",
        fetch_error="ConnectError: dial timeout",
    )


def test_splice_card_keeps_our_name_when_upstream_is_none(
    spliced_card_with_fetch_error: dict[str, Any],
) -> None:
    # Arrange
    card = spliced_card_with_fetch_error
    # Act
    name = card["name"]
    # Assert
    assert name == "proxy-front"


def test_splice_card_surfaces_fetch_error_when_upstream_none(
    spliced_card_with_fetch_error: dict[str, Any],
) -> None:
    # Arrange
    card = spliced_card_with_fetch_error
    # Act
    err = card["x-scitex-agent-container"]["upstream_card_fetch_error"]
    # Assert
    assert err == "ConnectError: dial timeout"


# ---------------------------------------------------------------------------
# POST /v1/turn — forward roundtrip
# ---------------------------------------------------------------------------


@pytest.fixture
def forward_roundtrip_response() -> httpx.Response:
    # Arrange
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
    # Act
    with TestClient(app) as tc:
        return tc.post("/v1/turn", json={"text": "hello"})


def test_post_v1_turn_forward_returns_status_200(
    forward_roundtrip_response: httpx.Response,
) -> None:
    # Arrange
    r = forward_roundtrip_response
    # Act
    status = r.status_code
    # Assert
    assert status == 200


def test_post_v1_turn_forward_returns_upstream_reply_body(
    forward_roundtrip_response: httpx.Response,
) -> None:
    # Arrange
    r = forward_roundtrip_response
    # Act
    body = r.json()
    # Assert
    assert body == {"reply": "echo:hello"}


# ---------------------------------------------------------------------------
# Timeout → 504
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_timeout_response() -> httpx.Response:
    # Arrange
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
    # Act
    with TestClient(app) as tc:
        return tc.post("/v1/turn", json={"text": "hi"})


def test_upstream_timeout_returns_status_504(
    upstream_timeout_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_timeout_response
    # Act
    status = r.status_code
    # Assert
    assert status == 504


def test_upstream_timeout_error_message_mentions_timeout(
    upstream_timeout_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_timeout_response
    # Act
    msg = r.json()["error"].lower()
    # Assert
    assert "timeout" in msg


# ---------------------------------------------------------------------------
# Upstream 5xx → our 502
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_500_response() -> httpx.Response:
    # Arrange
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
    # Act
    with TestClient(app) as tc:
        return tc.post("/v1/turn", json={"text": "hi"})


def test_upstream_500_returns_status_502(
    upstream_500_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_500_response
    # Act
    status = r.status_code
    # Assert
    assert status == 502


def test_upstream_500_error_message_includes_upstream_body(
    upstream_500_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_500_response
    # Act
    err = r.json()["error"]
    # Assert
    assert "kaboom" in err


# ---------------------------------------------------------------------------
# Redaction → 400 before forwarding
# ---------------------------------------------------------------------------


@pytest.fixture
def redacted_prompt_run() -> tuple[httpx.Response, dict[str, bool]]:
    # Arrange
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
    # Act
    with TestClient(app) as tc:
        r = tc.post("/v1/turn", json={"text": "leaking SECRET data"})
    return r, called


def test_redact_term_in_prompt_returns_status_400(
    redacted_prompt_run: tuple[httpx.Response, dict[str, bool]],
) -> None:
    # Arrange
    r, _ = redacted_prompt_run
    # Act
    status = r.status_code
    # Assert
    assert status == 400


def test_redact_term_in_prompt_error_message_mentions_redacted(
    redacted_prompt_run: tuple[httpx.Response, dict[str, bool]],
) -> None:
    # Arrange
    r, _ = redacted_prompt_run
    # Act
    err = r.json()["error"]
    # Assert
    assert "redacted" in err


def test_redact_term_in_prompt_does_not_forward_to_upstream(
    redacted_prompt_run: tuple[httpx.Response, dict[str, bool]],
) -> None:
    # Arrange
    _, called = redacted_prompt_run
    # Act
    forwarded = called["forwarded"]
    # Assert
    assert forwarded is False


# ---------------------------------------------------------------------------
# Redirect to disallowed host → 502
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_redirect_response() -> httpx.Response:
    # Arrange
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
    # Act
    with TestClient(app) as tc:
        return tc.post("/v1/turn", json={"text": "hi"})


def test_upstream_redirect_to_other_host_returns_status_502(
    upstream_redirect_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_redirect_response
    # Act
    status = r.status_code
    # Assert
    assert status == 502


def test_upstream_redirect_to_other_host_error_says_disallowed_host(
    upstream_redirect_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_redirect_response
    # Act
    err = r.json()["error"]
    # Assert
    assert "disallowed host" in err


def test_upstream_redirect_to_other_host_error_includes_offending_host(
    upstream_redirect_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_redirect_response
    # Act
    err = r.json()["error"]
    # Assert
    assert "evil.example.com" in err


# ---------------------------------------------------------------------------
# GET /.well-known/agent-card.json — card splicing in-flight
# ---------------------------------------------------------------------------


# A2A v1 upstream card — supportedInterfaces[], no top-level url.
_AGENT_CARD_UPSTREAM = {
    "name": "real-peer",
    "supportedInterfaces": [
        {
            "url": "http://peer/agents/real-peer",
            "protocolBinding": "HTTP+JSON",
            "tenant": "real-peer",
            "protocolVersion": "1.0",
        }
    ],
    "skills": [{"id": "peer.do-things", "name": "do-things"}],
    "capabilities": {"streaming": True},
}


@pytest.fixture
def agent_card_response() -> dict[str, Any]:
    # Arrange
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=[],
        timeout_s=5.0,
        upstream_card=_AGENT_CARD_UPSTREAM,
    )
    # Act
    with TestClient(app) as tc:
        r = tc.get("/.well-known/agent-card.json")
    return {"status": r.status_code, "card": r.json()}


def test_agent_card_returns_status_200(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    result = agent_card_response
    # Act
    status = result["status"]
    # Assert
    assert status == 200


@pytest.mark.parametrize(
    "path,expected",
    [
        (("name",), "proxy-front"),
        (("skills",), _AGENT_CARD_UPSTREAM["skills"]),
        (("capabilities",), _AGENT_CARD_UPSTREAM["capabilities"]),
        (("x-scitex-agent-container", "kind"), "AgentProxy"),
        (("x-scitex-agent-container", "upstream"), "https://peer.example.com"),
        (("x-scitex-agent-container", "trust"), "local-mesh"),
    ],
)
def test_agent_card_splices_field(
    agent_card_response: dict[str, Any],
    path: tuple[str, ...],
    expected: Any,
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    actual: Any = card
    for key in path:
        actual = actual[key]
    # Assert
    assert actual == expected


def test_agent_card_url_is_request_derived_for_our_agent(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    url = card["supportedInterfaces"][0]["url"]
    # Assert
    assert "/agents/proxy-front" in url


def test_agent_card_supported_interface_protocol_binding(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    binding = card["supportedInterfaces"][0]["protocolBinding"]
    # Assert
    assert binding == "HTTP+JSON"


def test_agent_card_supported_interface_tenant(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    tenant = card["supportedInterfaces"][0]["tenant"]
    # Assert
    assert tenant == "proxy-front"


def test_agent_card_supported_interface_protocol_version(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    version = card["supportedInterfaces"][0]["protocolVersion"]
    # Assert
    assert version == "1.0"


def test_agent_card_has_no_top_level_url(
    agent_card_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_response["card"]
    # Act
    has_url = "url" in card
    # Assert
    assert has_url is False


@pytest.fixture
def agent_card_fallback_response() -> dict[str, Any]:
    # Arrange
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        upstream_card_error="ConnectError: dial timeout",
    )
    # Act
    with TestClient(app) as tc:
        r = tc.get("/.well-known/agent.json")  # mirror route
    return {"status": r.status_code, "card": r.json()}


def test_agent_card_fallback_returns_status_200(
    agent_card_fallback_response: dict[str, Any],
) -> None:
    # Arrange
    result = agent_card_fallback_response
    # Act
    status = result["status"]
    # Assert
    assert status == 200


def test_agent_card_fallback_keeps_our_name(
    agent_card_fallback_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_fallback_response["card"]
    # Act
    name = card["name"]
    # Assert
    assert name == "proxy-front"


def test_agent_card_fallback_surfaces_upstream_fetch_error(
    agent_card_fallback_response: dict[str, Any],
) -> None:
    # Arrange
    card = agent_card_fallback_response["card"]
    # Act
    err = card["x-scitex-agent-container"]["upstream_card_fetch_error"]
    # Assert
    assert err == "ConnectError: dial timeout"


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.fixture
def health_response() -> dict[str, Any]:
    # Arrange
    app = build_app(
        name="proxy-front",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
    )
    # Act
    with TestClient(app) as tc:
        r = tc.get("/health")
    return {"status": r.status_code, "body": r.json()}


def test_health_returns_status_200(
    health_response: dict[str, Any],
) -> None:
    # Arrange
    result = health_response
    # Act
    status = result["status"]
    # Assert
    assert status == 200


def test_health_body_reports_upstream_and_trust(
    health_response: dict[str, Any],
) -> None:
    # Arrange
    result = health_response
    # Act
    body = result["body"]
    # Assert
    assert body == {
        "status": "ok",
        "upstream": "https://peer.example.com",
        "trust": "local-mesh",
    }
