"""Smoke tests for the inbound-turn HTTP endpoint.

Drives ``serve_inbound`` with a synthetic inbox + a tiny consumer task
that mimics the SDK conversation: dequeue envelope, set its future
with a canned reply. Asserts POST /v1/turn round-trips through the
queue and returns the reply as JSON.

TQ cleanup: every test carries AAA markers (TQ002), descriptive names
spell out the behaviour being verified (TQ003), and each test asserts
exactly one fact (TQ007). Repeated route/error matrices collapse into
``pytest.parametrize`` (TQ001). No mocks/monkeypatch — the consumer is
a real asyncio task that mirrors the conversation task contract, and
env-var isolation uses an explicit save/restore fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners._session_http import serve_inbound
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)

# ---------------------------------------------------------------------------
# Shared helpers — real collaborators, not mocks
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused TCP port (race-y but adequate for a test)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fake_consumer(inbox: "asyncio.Queue", *, reply_map: dict) -> None:
    """Stand in for the real conversation task: pop turn envelopes and
    resolve the future from a canned reply map. Real asyncio task, not
    a mock — mirrors the conversation task's envelope contract."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_result(reply_map.get(env.text, f"echo:{env.text}"))


async def _wait_bound(port: int) -> None:
    """Poll until the TCP port accepts connections (server has bound)."""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    pytest.fail(f"server never bound on port {port}")


async def _run_sidecar(
    *,
    port: int,
    reply_map: dict | None = None,
    agent_name: str = "",
    spec_yaml_path: str = "",
    client_coro,
) -> Any:
    """Spin up the sidecar + a real consumer task, run ``client_coro``
    once the server is bound, then shut everything down cleanly. Returns
    whatever ``client_coro(port)`` returned."""
    inbox = make_inbox()
    stop = asyncio.Event()
    consumer = asyncio.create_task(_fake_consumer(inbox, reply_map=reply_map or {}))
    server_kwargs: dict[str, Any] = {"host": "127.0.0.1", "port": port, "stop": stop}
    if agent_name:
        server_kwargs["agent_name"] = agent_name
    if spec_yaml_path:
        server_kwargs["spec_yaml_path"] = spec_yaml_path
    server = asyncio.create_task(serve_inbound(inbox, **server_kwargs))
    try:
        await _wait_bound(port)
        result = await client_coro(port)
    finally:
        stop.set()
        await inbox.put(ShutdownEnvelope())
        await asyncio.wait_for(consumer, timeout=5.0)
        await asyncio.wait_for(server, timeout=5.0)
    return result


def _http_post(url: str, body: bytes) -> tuple[int, dict | None]:
    """POST JSON to ``url`` — returns ``(status, parsed_body_or_None)``."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _http_get(url: str) -> tuple[int, dict | None]:
    """GET ``url`` — returns ``(status, parsed_body_or_None)``."""
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, None


# ---------------------------------------------------------------------------
# POST /v1/turn — the canonical inbound-turn channel
# ---------------------------------------------------------------------------


class TestServeInbound:
    def test_post_v1_turn_returns_status_200(self) -> None:
        """Happy-path round-trip: handler reaches consumer and replies 200."""
        # Arrange
        port = _free_port()
        reply_map = {"hello": "world"}

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post, f"http://127.0.0.1:{p}/v1/turn", b'{"text": "hello"}'
            )

        # Act
        status, _ = asyncio.run(
            _run_sidecar(port=port, reply_map=reply_map, client_coro=_client)
        )
        # Assert
        assert status == 200

    def test_post_v1_turn_returns_text_from_consumer(self) -> None:
        """Response body's ``text`` is the value the consumer resolved with."""
        # Arrange
        port = _free_port()
        reply_map = {"hello": "world"}

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post, f"http://127.0.0.1:{p}/v1/turn", b'{"text": "hello"}'
            )

        # Act
        _, body = asyncio.run(
            _run_sidecar(port=port, reply_map=reply_map, client_coro=_client)
        )
        # Assert
        assert body["text"] == "world"

    def test_post_v1_turn_returns_exit_after_false_by_default(self) -> None:
        """``exit_after`` defaults to False when the request omits the flag."""
        # Arrange
        port = _free_port()
        reply_map = {"hello": "world"}

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post, f"http://127.0.0.1:{p}/v1/turn", b'{"text": "hello"}'
            )

        # Act
        _, body = asyncio.run(
            _run_sidecar(port=port, reply_map=reply_map, client_coro=_client)
        )
        # Assert
        assert body["exit_after"] is False

    def test_post_v1_turn_with_missing_text_returns_400(self) -> None:
        """A body without ``text`` is rejected with a 400."""
        # Arrange
        port = _free_port()

        async def _client(p: int):
            return await asyncio.to_thread(
                _http_post, f"http://127.0.0.1:{p}/v1/turn", b"{}"
            )

        # Act
        status, _ = asyncio.run(_run_sidecar(port=port, client_coro=_client))
        # Assert
        assert status == 400

    def test_health_endpoint_returns_status_ok(self) -> None:
        """GET /health returns the canonical readiness body."""
        # Arrange
        port = _free_port()

        async def _client(p: int):
            return await asyncio.to_thread(_http_get, f"http://127.0.0.1:{p}/health")

        # Act
        _, body = asyncio.run(_run_sidecar(port=port, client_coro=_client))
        # Assert
        assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# /.well-known/agent-card.json — A2A AgentCard discovery
# ---------------------------------------------------------------------------


_CARD_PATH = "/.well-known/agent-card.json"

_VALID_YAML = (
    "apiVersion: scitex-agent-container/v3\n"
    "kind: Agent\n"
    "metadata:\n"
    "  labels:\n"
    "    role: ecosystem-auditor\n"
    "    team: lab-a\n"
    "spec:\n"
    "  runtime: apptainer\n"
    "  host: ${HOSTNAME}\n"
    "  workdir: /home/agent/work\n"
    "  apptainer:\n    image: /x.sif\n    binds: []\n"
    "  claude:\n    model: sonnet\n"
    "  health:\n    enabled: true\n    interval: 60\n"
    "  restart:\n    policy: on-failure\n    max_retries: 3\n"
)


@pytest.fixture
def auditor_yaml(tmp_path: Path) -> Path:
    """Write a minimal v3 spec.yaml for the ``ecosystem-auditor`` role."""
    yaml_path = tmp_path / "ecosystem-auditor" / "spec.yaml"
    yaml_path.parent.mkdir()
    yaml_path.write_text(_VALID_YAML)
    return yaml_path


@pytest.fixture
def minimal_yaml(tmp_path: Path) -> Path:
    """Write the smallest viable v3 spec.yaml at the test's tmp_path root."""
    yaml_path = tmp_path / "spec.yaml"
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
    )
    return yaml_path


def _fetch_card(*, agent_name: str, spec_yaml_path: str) -> tuple[int, dict | None]:
    """Spin up the sidecar for an AgentCard fetch and return ``(status, body)``."""
    port = _free_port()

    async def _client(p: int):
        return await asyncio.to_thread(_http_get, f"http://127.0.0.1:{p}{_CARD_PATH}")

    return asyncio.run(
        _run_sidecar(
            port=port,
            agent_name=agent_name,
            spec_yaml_path=spec_yaml_path,
            client_coro=_client,
        )
    )


@pytest.fixture
def sac_listen_base_url_env():
    """Save/restore ``SAC_LISTEN_BASE_URL`` around a test."""
    saved = os.environ.get("SAC_LISTEN_BASE_URL")

    def _set(value: str | None) -> None:
        if value is None:
            os.environ.pop("SAC_LISTEN_BASE_URL", None)
        else:
            os.environ["SAC_LISTEN_BASE_URL"] = value

    yield _set
    if saved is None:
        os.environ.pop("SAC_LISTEN_BASE_URL", None)
    else:
        os.environ["SAC_LISTEN_BASE_URL"] = saved


class TestAgentCard:
    def test_well_known_card_returns_status_200(self, auditor_yaml: Path) -> None:
        """A valid spec yields an HTTP 200 on the well-known path."""
        # Arrange
        agent_name = "ecosystem-auditor"
        # Act
        status, _ = _fetch_card(agent_name=agent_name, spec_yaml_path=str(auditor_yaml))
        # Assert
        assert status == 200

    def test_well_known_card_name_matches_agent(self, auditor_yaml: Path) -> None:
        """Card's top-level ``name`` matches the runner's ``agent_name``."""
        # Arrange
        agent_name = "ecosystem-auditor"
        # Act
        _, body = _fetch_card(agent_name=agent_name, spec_yaml_path=str(auditor_yaml))
        # Assert
        assert body["name"] == agent_name

    @pytest.mark.parametrize("field", ["capabilities", "skills"])
    def test_well_known_card_includes_required_a2a_field(
        self, auditor_yaml: Path, field: str
    ) -> None:
        """Spec-required AgentCard fields per A2A are present in the body."""
        # Arrange
        agent_name = "ecosystem-auditor"
        # Act
        _, body = _fetch_card(agent_name=agent_name, spec_yaml_path=str(auditor_yaml))
        # Assert
        assert field in body

    def test_well_known_card_exposes_role_class_extension(
        self, auditor_yaml: Path
    ) -> None:
        """The x-scitex-agent-container extension surfaces ``role_class``."""
        # Arrange
        agent_name = "ecosystem-auditor"
        # Act
        _, body = _fetch_card(agent_name=agent_name, spec_yaml_path=str(auditor_yaml))
        # Assert
        assert body["x-scitex-agent-container"]["role_class"] == agent_name

    def test_well_known_card_returns_404_when_yaml_path_unset(self) -> None:
        """Sidecar launched without --a2a-card-yaml → 404 on the card path."""
        # Arrange
        agent_name = "anything"
        # Act
        status, _ = _fetch_card(agent_name=agent_name, spec_yaml_path="")
        # Assert
        assert status == 404

    def test_well_known_card_returns_500_when_yaml_path_missing(
        self, tmp_path: Path
    ) -> None:
        """YAML path supplied but file is missing → 500 (server doesn't crash)."""
        # Arrange
        missing = tmp_path / "nope" / "spec.yaml"
        # Act
        status, _ = _fetch_card(agent_name="anything", spec_yaml_path=str(missing))
        # Assert
        assert status == 500

    def test_card_url_uses_sac_listen_base_url_env(
        self, minimal_yaml: Path, sac_listen_base_url_env
    ) -> None:
        """When ``SAC_LISTEN_BASE_URL`` is set, the card's per-agent URL
        uses that base — NOT the runner's volatile port. ADR-0004: URL
        lives under ``supportedInterfaces[0].url``."""
        # Arrange
        sac_listen_base_url_env("http://127.0.0.1:7878")
        agent_name = "ecosystem-auditor"
        expected = "http://127.0.0.1:7878/agents/ecosystem-auditor"
        # Act
        _, body = _fetch_card(agent_name=agent_name, spec_yaml_path=str(minimal_yaml))
        # Assert
        assert body["supportedInterfaces"][0]["url"] == expected

    def test_card_url_falls_back_to_request_base_when_env_unset(
        self, minimal_yaml: Path, sac_listen_base_url_env
    ) -> None:
        """Without the env override the card URL is built from
        ``request.base_url`` (keeps direct ``curl`` working in tests)."""
        # Arrange
        sac_listen_base_url_env(None)
        agent_name = "auditor"
        # Act
        _, body = _fetch_card(agent_name=agent_name, spec_yaml_path=str(minimal_yaml))
        per_agent_url = body["supportedInterfaces"][0]["url"]
        # Assert
        assert per_agent_url.startswith("http://127.0.0.1:") and per_agent_url.endswith(
            "/agents/auditor"
        )


# ---------------------------------------------------------------------------
# Name-in-path routes — sidecar mirrors `sac listen`'s URL shape.
#
# The AgentCard advertises ``url: <base>/agents/<name>`` so a client
# POSTing to the discovered URL must succeed. Regression for that wart.
# ---------------------------------------------------------------------------


def _post_turn(*, agent_name: str, path: str, text: str = "hi") -> int:
    """Spin up the sidecar with a fake consumer, POST JSON to ``path``,
    and return the HTTP status code."""
    port = _free_port()
    reply_map = {text: "ack"}

    async def _client(p: int):
        body = json.dumps({"text": text}).encode()
        status, _ = await asyncio.to_thread(
            _http_post, f"http://127.0.0.1:{p}{path}", body
        )
        return status

    return asyncio.run(
        _run_sidecar(
            port=port,
            reply_map=reply_map,
            agent_name=agent_name,
            client_coro=_client,
        )
    )


class TestNameInPathRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/agents/alpha/turn",  # canonical sac path
            "/agents/alpha/send",  # matches `sac listen`'s verb
            "/v1/turn",  # legacy shortcut
        ],
        ids=["canonical_turn", "canonical_send", "legacy_bare_turn"],
    )
    def test_matching_name_route_returns_200(self, path: str) -> None:
        """Routes whose path-name matches the agent on this port succeed."""
        # Arrange
        agent_name = "alpha"
        # Act
        status = _post_turn(agent_name=agent_name, path=path)
        # Assert
        assert status == 200

    def test_mismatched_name_in_path_returns_404(self) -> None:
        """If the URL's name doesn't match the agent on this port, 404 —
        sanity check, since port routing already pinned us."""
        # Arrange
        agent_name = "alpha"
        wrong_path = "/agents/beta/turn"
        # Act
        status = _post_turn(agent_name=agent_name, path=wrong_path)
        # Assert
        assert status == 404
