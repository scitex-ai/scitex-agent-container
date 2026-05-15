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


# ---------------------------------------------------------------------------
# _upstream_base — well-known suffix stripping
# ---------------------------------------------------------------------------


def test_upstream_base_strips_v1_well_known_suffix() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _upstream_base

    upstream = "https://peer.example.com/.well-known/agent-card.json"
    # Act
    base = _upstream_base(upstream)
    # Assert
    assert base == "https://peer.example.com"


def test_upstream_base_strips_legacy_well_known_suffix() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _upstream_base

    upstream = "https://peer.example.com/.well-known/agent.json"
    # Act
    base = _upstream_base(upstream)
    # Assert
    assert base == "https://peer.example.com"


# ---------------------------------------------------------------------------
# POST /v1/turn — bad JSON / HTTPError / non-JSON upstream reply
# ---------------------------------------------------------------------------


@pytest.fixture
def bad_json_post_response() -> httpx.Response:
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
        return tc.post(
            "/v1/turn",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )


def test_post_v1_turn_bad_json_returns_status_400(
    bad_json_post_response: httpx.Response,
) -> None:
    # Arrange
    r = bad_json_post_response
    # Act
    status = r.status_code
    # Assert
    assert status == 400


def test_post_v1_turn_bad_json_error_mentions_json(
    bad_json_post_response: httpx.Response,
) -> None:
    # Arrange
    r = bad_json_post_response
    # Act
    err = r.json()["error"].lower()
    # Assert
    assert "bad json" in err


@pytest.fixture
def upstream_http_error_response() -> httpx.Response:
    # Arrange
    async def boom(request: Request) -> Response:
        raise httpx.ConnectError("conn refused")

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


def test_upstream_connect_error_returns_status_502(
    upstream_http_error_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_http_error_response
    # Act
    status = r.status_code
    # Assert
    assert status == 502


def test_upstream_connect_error_mentions_unreachable(
    upstream_http_error_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_http_error_response
    # Act
    err = r.json()["error"]
    # Assert
    assert "unreachable" in err


@pytest.fixture
def upstream_non_json_response() -> httpx.Response:
    # Arrange
    async def plain(request: Request) -> Response:
        return Response("plain-text-reply", status_code=200)

    upstream = _make_upstream(turn_handler=plain)
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


def test_upstream_non_json_wraps_text_as_reply_field(
    upstream_non_json_response: httpx.Response,
) -> None:
    # Arrange
    r = upstream_non_json_response
    # Act
    body = r.json()
    # Assert
    assert body == {"reply": "plain-text-reply"}


# ---------------------------------------------------------------------------
# Real local upstream server (uvicorn on ephemeral port, background thread)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def real_upstream_server() -> Any:
    """Spin up an actual uvicorn server with a small Starlette upstream.

    Yields ``("http://127.0.0.1:<port>", recorder)`` where recorder is
    a dict the upstream mutates so tests can observe traffic.
    """
    import threading
    import time

    import uvicorn

    recorder: dict[str, Any] = {"hits": 0}

    async def post_turn(request: Request) -> JSONResponse:
        recorder["hits"] += 1
        body = await request.json()
        return JSONResponse({"reply": f"echo:{body.get('text', '')}"})

    async def get_card(request: Request) -> JSONResponse:
        return JSONResponse({"name": "real-peer", "skills": []})

    app = Starlette(
        routes=[
            Route("/v1/turn", post_turn, methods=["POST"]),
            Route("/.well-known/agent-card.json", get_card, methods=["GET"]),
        ]
    )

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until the server reports it's actually listening.
    deadline = time.time() + 5.0
    while time.time() < deadline and not server.started:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("upstream uvicorn server failed to start")

    try:
        yield (f"http://127.0.0.1:{port}", recorder)
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


@pytest.fixture
def failing_upstream_server() -> Any:
    """Real uvicorn server whose card endpoint returns 500."""
    import threading
    import time

    import uvicorn

    async def bad_card(request: Request) -> Response:
        return Response("boom", status_code=500)

    app = Starlette(
        routes=[Route("/.well-known/agent-card.json", bad_card, methods=["GET"])]
    )

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5.0
    while time.time() < deadline and not server.started:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("upstream uvicorn server failed to start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Fresh-client path (httpx_client=None) — talks to real local server
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_client_post_response(real_upstream_server: Any) -> httpx.Response:
    # Arrange
    base, _recorder = real_upstream_server
    app = build_app(
        name="proxy-front",
        upstream=base,
        trust="untrusted",
        redact=[],
        timeout_s=5.0,
        upstream_card=None,
        httpx_client=None,
    )
    # Act
    with TestClient(app) as tc:
        resp = tc.post("/v1/turn", json={"text": "hi"})
    # Assert (deferred)
    return resp


def test_fresh_client_post_returns_status_200(
    fresh_client_post_response: httpx.Response,
) -> None:
    # Arrange
    r = fresh_client_post_response
    # Act
    status = r.status_code
    # Assert
    assert status == 200


def test_fresh_client_post_returns_echo_body(
    fresh_client_post_response: httpx.Response,
) -> None:
    # Arrange
    r = fresh_client_post_response
    # Act
    body = r.json()
    # Assert
    assert body == {"reply": "echo:hi"}


# ---------------------------------------------------------------------------
# _fetch_upstream_card — real local server
# ---------------------------------------------------------------------------


@pytest.fixture
def fetched_upstream_card(real_upstream_server: Any) -> tuple[Any, str]:
    # Arrange
    import asyncio as _asyncio

    from scitex_agent_container._runners.a2a_proxy import _fetch_upstream_card

    base, _recorder = real_upstream_server
    # Act
    result = _asyncio.run(_fetch_upstream_card(base, timeout_s=5.0))
    # Assert (deferred)
    return result


def test_fetch_upstream_card_returns_card_dict(
    fetched_upstream_card: tuple[Any, str],
) -> None:
    # Arrange
    card, _err = fetched_upstream_card
    # Act
    name = card["name"]
    # Assert
    assert name == "real-peer"


def test_fetch_upstream_card_returns_empty_error_on_success(
    fetched_upstream_card: tuple[Any, str],
) -> None:
    # Arrange
    _card, err = fetched_upstream_card
    # Act
    actual = err
    # Assert
    assert actual == ""


@pytest.fixture
def fetched_upstream_card_failure(failing_upstream_server: Any) -> tuple[Any, str]:
    # Arrange
    import asyncio as _asyncio

    from scitex_agent_container._runners.a2a_proxy import _fetch_upstream_card

    base = failing_upstream_server
    # Act
    result = _asyncio.run(_fetch_upstream_card(base, timeout_s=5.0))
    # Assert (deferred)
    return result


def test_fetch_upstream_card_returns_none_on_failure(
    fetched_upstream_card_failure: tuple[Any, str],
) -> None:
    # Arrange
    card, _err = fetched_upstream_card_failure
    # Act
    actual = card
    # Assert
    assert actual is None


def test_fetch_upstream_card_returns_error_message_on_failure(
    fetched_upstream_card_failure: tuple[Any, str],
) -> None:
    # Arrange
    _card, err = fetched_upstream_card_failure
    # Act
    actual = err
    # Assert
    assert actual != ""


# ---------------------------------------------------------------------------
# _parse_argv — CLI argument parsing
# ---------------------------------------------------------------------------


def test_parse_argv_required_name_and_upstream_set() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _parse_argv

    argv = ["--name", "px", "--upstream", "http://u"]
    # Act
    ns = _parse_argv(argv)
    # Assert
    assert (ns.name, ns.upstream) == ("px", "http://u")


def test_parse_argv_defaults_trust_to_untrusted() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _parse_argv

    argv = ["--name", "px", "--upstream", "http://u"]
    # Act
    ns = _parse_argv(argv)
    # Assert
    assert ns.trust == "untrusted"


def test_parse_argv_accepts_redact_csv_string() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _parse_argv

    argv = ["--name", "px", "--upstream", "http://u", "--redact", "a,b,c"]
    # Act
    ns = _parse_argv(argv)
    # Assert
    assert ns.redact == "a,b,c"


def test_parse_argv_accepts_a2a_port_int() -> None:
    # Arrange
    from scitex_agent_container._runners.a2a_proxy import _parse_argv

    argv = ["--name", "px", "--upstream", "http://u", "--a2a-port", "7901"]
    # Act
    ns = _parse_argv(argv)
    # Assert
    assert ns.a2a_port == 7901


# ---------------------------------------------------------------------------
# run() lifecycle — pid + heartbeat written, terminates on SIGTERM
#
# Drives the real ``run`` coroutine against the real local upstream
# (so ``_fetch_upstream_card`` actually fetches the upstream's card),
# then issues SIGTERM to itself to exercise the signal-handler /
# shutdown / heartbeat-stopping path.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_lifecycle_state_dir(tmp_path: Any, real_upstream_server: Any) -> Any:
    # Arrange
    import asyncio as _asyncio
    import os
    import signal

    from scitex_agent_container._runners import a2a_proxy as _mod

    base, _recorder = real_upstream_server
    state_root = tmp_path / "rt"

    async def _drive() -> int:
        task = _asyncio.create_task(
            _mod.run(
                "px",
                upstream=base,
                trust="untrusted",
                redact=[],
                timeout_s=1.0,
                state_root=state_root,
                tick_seconds=0.05,
                a2a_host="127.0.0.1",
                a2a_port=None,
            )
        )
        # Let the runner write pid + heartbeat, then signal stop.
        await _asyncio.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)
        return await _asyncio.wait_for(task, timeout=5.0)

    # Act
    rc = _asyncio.run(_drive())
    # Assert (deferred)
    return {"rc": rc, "state_dir": state_root / "px"}


def test_run_returns_zero_on_clean_shutdown(
    run_lifecycle_state_dir: dict[str, Any],
) -> None:
    # Arrange
    result = run_lifecycle_state_dir
    # Act
    rc = result["rc"]
    # Assert
    assert rc == 0


def test_run_writes_pid_file_in_state_dir(
    run_lifecycle_state_dir: dict[str, Any],
) -> None:
    # Arrange
    state_dir = run_lifecycle_state_dir["state_dir"]
    # Act
    pid_file = state_dir / "pid"
    # Assert
    assert pid_file.is_file()


def test_run_writes_heartbeat_file_in_state_dir(
    run_lifecycle_state_dir: dict[str, Any],
) -> None:
    # Arrange
    state_dir = run_lifecycle_state_dir["state_dir"]
    # Act
    hb_file = state_dir / "heartbeat.json"
    # Assert
    assert hb_file.is_file()


def test_run_final_heartbeat_state_is_stopping(
    run_lifecycle_state_dir: dict[str, Any],
) -> None:
    # Arrange
    import json as _json

    state_dir = run_lifecycle_state_dir["state_dir"]
    # Act
    hb = _json.loads((state_dir / "heartbeat.json").read_text())
    # Assert
    assert hb["state"] == "stopping"


# ---------------------------------------------------------------------------
# main() — CLI entry; drives a real (short-lived) run() against the
# real local upstream, terminated by a self-issued SIGTERM from a
# background thread once we know the runner is up.
# ---------------------------------------------------------------------------


@pytest.fixture
def main_short_run(tmp_path: Any, real_upstream_server: Any) -> dict[str, Any]:
    # Arrange
    import os
    import signal
    import threading
    import time

    from scitex_agent_container._runners.a2a_proxy import main as _main

    base, _recorder = real_upstream_server
    state_root = tmp_path / "rt"
    target_pid = os.getpid()

    def _shoot() -> None:
        # Give run() time to install the signal handler, then SIGTERM.
        time.sleep(0.3)
        os.kill(target_pid, signal.SIGTERM)

    killer = threading.Thread(target=_shoot, daemon=True)
    killer.start()

    # Act
    rc = _main(
        [
            "--name",
            "px-main",
            "--upstream",
            base,
            "--redact",
            "a, ,b",
            "--state-root",
            str(state_root),
            "--tick-seconds",
            "0.05",
        ]
    )
    killer.join(timeout=2.0)
    # Assert (deferred)
    return {"rc": rc, "state_dir": state_root / "px-main"}


def test_main_returns_zero_on_clean_shutdown(
    main_short_run: dict[str, Any],
) -> None:
    # Arrange
    result = main_short_run
    # Act
    rc = result["rc"]
    # Assert
    assert rc == 0


def test_main_writes_pid_under_state_root(
    main_short_run: dict[str, Any],
) -> None:
    # Arrange
    state_dir = main_short_run["state_dir"]
    # Act
    pid_file = state_dir / "pid"
    # Assert
    assert pid_file.is_file()


# ---------------------------------------------------------------------------
# run() with --a2a-port — exercises the uvicorn-server branch so the
# real proxy serves /health on the bound port, then SIGTERM cancels the
# serve task to cover the shutdown cleanup path.
# ---------------------------------------------------------------------------


@pytest.fixture
def run_with_bound_port(tmp_path: Any, real_upstream_server: Any) -> dict[str, Any]:
    # Arrange
    import asyncio as _asyncio
    import os
    import signal

    from scitex_agent_container._runners import a2a_proxy as _mod

    base, _recorder = real_upstream_server
    state_root = tmp_path / "rt"
    proxy_port = _free_port()
    health_status: dict[str, Any] = {"code": None}

    async def _drive() -> int:
        task = _asyncio.create_task(
            _mod.run(
                "px-bound",
                upstream=base,
                trust="untrusted",
                redact=[],
                timeout_s=1.0,
                state_root=state_root,
                tick_seconds=0.05,
                a2a_host="127.0.0.1",
                a2a_port=proxy_port,
            )
        )
        # Wait for the proxy to be ready by hitting /health.
        async with httpx.AsyncClient(timeout=2.0) as c:
            for _ in range(50):
                try:
                    r = await c.get(f"http://127.0.0.1:{proxy_port}/health")
                    if r.status_code == 200:
                        health_status["code"] = r.status_code
                        break
                except (
                    httpx.HTTPError
                ):  # stx-allow: fallback (reason: server still booting)
                    pass
                await _asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        return await _asyncio.wait_for(task, timeout=5.0)

    # Act
    rc = _asyncio.run(_drive())
    # Assert (deferred)
    return {"rc": rc, "health_code": health_status["code"]}


def test_run_with_bound_port_returns_zero(
    run_with_bound_port: dict[str, Any],
) -> None:
    # Arrange
    result = run_with_bound_port
    # Act
    rc = result["rc"]
    # Assert
    assert rc == 0


def test_run_with_bound_port_serves_health_endpoint(
    run_with_bound_port: dict[str, Any],
) -> None:
    # Arrange
    result = run_with_bound_port
    # Act
    code = result["health_code"]
    # Assert
    assert code == 200
