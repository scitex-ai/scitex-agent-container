"""Bounded-timeout + A2A v1 body-shape tests for ``POST /v1/turn``.

The lead observed that proj-scitex-stats running on spartan-bm043 would
accept a turn but never return — ``curl -X POST .../v1/turn`` blocked
forever, the lead's ssh+curl pipeline ate its 600 s ceiling, and the
SDK's actual completion was only visible in session.jsonl. Without a
bounded handler-side wait the HTTP caller has no failure surface.

This module pins the contract that:

* the handler uses a BOUNDED ``asyncio.wait_for``,
* the cap defaults to 120 s and is overridable via
  ``SAC_A2A_TURN_TIMEOUT_S``,
* a tripped cap returns ``504`` with an error body that names the
  timeout value and the (possibly ``None``) ``session_id``, and
* a successful turn returns ``200`` with the A2A-v1-style ``text`` key
  plus the ``session_id`` the SDK reported.

Each test follows AAA structure (STX-TQ002) with one substantive
assert (STX-TQ007). No monkeypatch / mocker (STX-NM002) — collaborators
are real ``serve_inbound`` + a real asyncio consumer that mimics the
conversation task's envelope contract, exactly like the existing
``test__session_http.py`` suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest

from scitex_agent_container._runners._session_http import (
    DEFAULT_TURN_TIMEOUT_S,
    TURN_TIMEOUT_ENV_VAR,
    serve_inbound,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)

# ---------------------------------------------------------------------------
# Real-collaborator helpers (mirror test__session_http.py)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_bound(port: int) -> None:
    """Poll until the TCP port accepts connections."""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    pytest.fail(f"server never bound on port {port}")


def _http_post(url: str, body: bytes) -> tuple[int, dict | None]:
    """POST JSON to ``url`` — returns ``(status, parsed_body_or_None)``.

    On a 4xx/5xx the body is parsed too — we need to inspect 504/400
    error envelopes, not just status codes.
    """
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except (
            ValueError,
            OSError,
        ):  # stx-allow: fallback (reason: error body may be empty / malformed in test)
            payload = None
        return exc.code, payload


# Fake-consumer flavors — real asyncio tasks, not mocks. Each mirrors the
# real conversation-task contract over ``TurnEnvelope``.


async def _consumer_resolves_immediately(
    inbox: "asyncio.Queue", *, reply: str, session_id: str | None
) -> None:
    """Pop envelopes and resolve with ``reply``; tag ``session_id``."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.session_id = session_id
            env.response.set_result(reply)


async def _consumer_never_resolves(inbox: "asyncio.Queue") -> None:
    """Pop envelopes and DROP them on the floor — future never resolves.

    This mimics a wedged SDK turn: the envelope reached the consumer
    but the consumer never settles the future. The HTTP handler must
    trip its ``asyncio.wait_for`` cap and answer 504.
    """
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        # Intentionally NOT resolving env.response.


async def _run_sidecar(
    *,
    port: int,
    consumer_factory,
    turn_timeout_s: float | None,
    client_coro,
) -> Any:
    """Spin up the sidecar + a consumer + run ``client_coro``.

    ``consumer_factory(inbox)`` returns the awaitable consumer task body.
    """
    inbox = make_inbox()
    stop = asyncio.Event()
    consumer = asyncio.create_task(consumer_factory(inbox))
    server_kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "stop": stop,
    }
    if turn_timeout_s is not None:
        server_kwargs["turn_timeout_s"] = turn_timeout_s
    server = asyncio.create_task(serve_inbound(inbox, **server_kwargs))
    try:
        await _wait_bound(port)
        result = await client_coro(port)
    finally:
        stop.set()
        await inbox.put(ShutdownEnvelope())
        try:
            await asyncio.wait_for(consumer, timeout=5.0)
        except asyncio.TimeoutError:
            consumer.cancel()
            try:
                await consumer
            except (
                asyncio.CancelledError,
                Exception,
            ):  # stx-allow: fallback (reason: defensive cleanup at test teardown)
                pass
        await asyncio.wait_for(server, timeout=5.0)
    return result


@pytest.fixture
def turn_timeout_env():
    """Save/restore ``SAC_A2A_TURN_TIMEOUT_S`` around a test (no monkeypatch)."""
    saved = os.environ.get(TURN_TIMEOUT_ENV_VAR)

    def _set(value: str | None) -> None:
        if value is None:
            os.environ.pop(TURN_TIMEOUT_ENV_VAR, None)
        else:
            os.environ[TURN_TIMEOUT_ENV_VAR] = value

    yield _set
    if saved is None:
        os.environ.pop(TURN_TIMEOUT_ENV_VAR, None)
    else:
        os.environ[TURN_TIMEOUT_ENV_VAR] = saved


# ---------------------------------------------------------------------------
# Happy-path turn — bounded wait, A2A-style body
# ---------------------------------------------------------------------------


def _run_happy_turn(
    *, port: int, reply: str, session_id: str | None, turn_timeout_s: float
) -> tuple[int, dict | None]:
    """Drive one POST /v1/turn against a fast-resolving consumer."""

    async def _client(p: int):
        return await asyncio.to_thread(
            _http_post,
            f"http://127.0.0.1:{p}/v1/turn",
            b'{"text": "hello"}',
        )

    return asyncio.run(
        _run_sidecar(
            port=port,
            consumer_factory=lambda ib: _consumer_resolves_immediately(
                ib, reply=reply, session_id=session_id
            ),
            turn_timeout_s=turn_timeout_s,
            client_coro=_client,
        )
    )


class TestHappyTurn:
    def test_turn_returns_response_within_timeout(self) -> None:
        """Fast-resolving SDK consumer returns 200 well inside the cap."""
        # Arrange
        port = _free_port()

        # Act
        status, _ = _run_happy_turn(
            port=port,
            reply="hi",
            session_id="sid-fast",
            turn_timeout_s=5.0,
        )

        # Assert
        assert status == 200

    def test_turn_200_body_has_a2a_text_field(self) -> None:
        """200 body exposes the canonical A2A ``text`` field."""
        # Arrange
        port = _free_port()

        # Act
        _, body = _run_happy_turn(
            port=port,
            reply="hi",
            session_id="sid-fast",
            turn_timeout_s=5.0,
        )

        # Assert
        assert body["text"] == "hi"

    def test_turn_response_body_includes_session_id(self) -> None:
        """The 200 body echoes the SDK ``session_id`` the consumer set."""
        # Arrange
        port = _free_port()

        # Act
        _, body = _run_happy_turn(
            port=port,
            reply="ok",
            session_id="sid-abc-123",
            turn_timeout_s=5.0,
        )

        # Assert
        assert body["session_id"] == "sid-abc-123"


# ---------------------------------------------------------------------------
# 504 on bounded-wait timeout — wedged SDK turn must surface loudly
# ---------------------------------------------------------------------------


def _run_timeout_turn(*, port: int, turn_timeout_s: float) -> tuple[int, dict | None]:
    """Drive one POST /v1/turn against a consumer that never resolves."""

    async def _client(p: int):
        return await asyncio.to_thread(
            _http_post,
            f"http://127.0.0.1:{p}/v1/turn",
            b'{"text": "wedge me"}',
        )

    return asyncio.run(
        _run_sidecar(
            port=port,
            consumer_factory=_consumer_never_resolves,
            turn_timeout_s=turn_timeout_s,
            client_coro=_client,
        )
    )


class TestTurnTimeout:
    def test_turn_504_on_sdk_timeout(self) -> None:
        """Wedged consumer + tight cap → HTTP 504 (not a hang)."""
        # Arrange
        port = _free_port()
        cap = 0.3

        # Act
        status, _ = _run_timeout_turn(port=port, turn_timeout_s=cap)

        # Assert
        assert status == 504

    def test_turn_504_body_includes_timeout_seconds(self) -> None:
        """504 body carries the integer-formatted timeout it tripped on."""
        # Arrange
        port = _free_port()
        cap = 0.3

        # Act
        _, body = _run_timeout_turn(port=port, turn_timeout_s=cap)

        # Assert
        assert "0s timeout" in body["error"]

    def test_turn_504_body_carries_timeout_s_field(self) -> None:
        """504 body also exposes the cap as a numeric ``timeout_s`` field."""
        # Arrange
        port = _free_port()
        cap = 0.3

        # Act
        _, body = _run_timeout_turn(port=port, turn_timeout_s=cap)

        # Assert
        assert body["timeout_s"] == cap

    def test_turn_504_body_includes_session_id_field(self) -> None:
        """504 body carries a ``session_id`` key (None when SDK hadn't said yet)."""
        # Arrange
        port = _free_port()
        cap = 0.3

        # Act
        _, body = _run_timeout_turn(port=port, turn_timeout_s=cap)

        # Assert
        assert body["session_id"] is None


# ---------------------------------------------------------------------------
# 400 on missing ``text`` field — schema mismatch must be loud, not a hang
# ---------------------------------------------------------------------------


def _run_missing_text_turn(*, port: int) -> tuple[int, dict | None]:
    """POST a body without ``text`` (e.g. ``{"prompt": "..."}``)."""

    async def _client(p: int):
        return await asyncio.to_thread(
            _http_post,
            f"http://127.0.0.1:{p}/v1/turn",
            b'{"prompt": "wrong key"}',
        )

    return asyncio.run(
        _run_sidecar(
            port=port,
            consumer_factory=lambda ib: _consumer_resolves_immediately(
                ib, reply="never", session_id=None
            ),
            turn_timeout_s=5.0,
            client_coro=_client,
        )
    )


class TestTurnMissingText:
    def test_turn_400_on_missing_text_field(self) -> None:
        """A body without ``text`` (e.g. ``prompt``) is rejected with 400."""
        # Arrange
        port = _free_port()

        # Act
        status, _ = _run_missing_text_turn(port=port)

        # Assert
        assert status == 400

    def test_turn_400_body_says_missing_text(self) -> None:
        """400 body names the missing ``text`` field so the caller can fix it."""
        # Arrange
        port = _free_port()

        # Act
        _, body = _run_missing_text_turn(port=port)

        # Assert
        assert "'text' field" in body["error"]


# ---------------------------------------------------------------------------
# Env-var override + default — the bounded-wait knob must be tunable.
# ---------------------------------------------------------------------------


class TestTurnTimeoutEnv:
    def test_default_turn_timeout_is_120_seconds(self) -> None:
        """The documented default is 120 s (sidecar contract, not the SDK's)."""
        # Arrange
        expected = 120.0
        # Act
        actual = DEFAULT_TURN_TIMEOUT_S
        # Assert
        assert actual == expected

    def test_env_override_changes_effective_timeout(self, turn_timeout_env) -> None:
        """Setting ``SAC_A2A_TURN_TIMEOUT_S`` overrides the default cap."""
        # Arrange
        turn_timeout_env("0.25")
        port = _free_port()

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post,
                f"http://127.0.0.1:{p}/v1/turn",
                b'{"text": "wedge"}',
            )

        # Act
        status, _ = asyncio.run(
            _run_sidecar(
                port=port,
                consumer_factory=_consumer_never_resolves,
                turn_timeout_s=None,  # rely on env
                client_coro=_client,
            )
        )

        # Assert
        assert status == 504

    def test_env_override_value_appears_in_504_body(self, turn_timeout_env) -> None:
        """Effective cap propagates into the 504 error envelope's value."""
        # Arrange
        turn_timeout_env("0.25")
        port = _free_port()

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post,
                f"http://127.0.0.1:{p}/v1/turn",
                b'{"text": "wedge"}',
            )

        # Act
        _, body = asyncio.run(
            _run_sidecar(
                port=port,
                consumer_factory=_consumer_never_resolves,
                turn_timeout_s=None,
                client_coro=_client,
            )
        )

        # Assert
        assert body["timeout_s"] == 0.25

    def test_malformed_env_var_raises_loudly(self, turn_timeout_env) -> None:
        """A non-numeric env value MUST raise — not silently fall back."""
        # Arrange
        from scitex_agent_container._runners._session_http import (
            _resolve_turn_timeout,
        )

        turn_timeout_env("not-a-number")
        raised: BaseException | None = None
        # Act
        try:
            _resolve_turn_timeout(None)
        except ValueError as exc:
            raised = exc
        # Assert
        assert isinstance(raised, ValueError)
