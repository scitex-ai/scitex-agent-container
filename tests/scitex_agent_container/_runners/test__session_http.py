"""Smoke tests for the inbound-turn HTTP endpoint.

Drives ``serve_inbound`` with a synthetic inbox + a tiny consumer task
that mimics the SDK conversation: dequeue envelope, set its future
with a canned reply. Asserts POST /v1/turn round-trips through the
queue and returns the reply as JSON.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from scitex_agent_container._runners._session_http import serve_inbound
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)


def _free_port() -> int:
    """Ask the kernel for an unused TCP port (race-y but adequate for a test)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fake_consumer(inbox: "asyncio.Queue", *, reply_map: dict) -> None:
    """Stand in for the real conversation task: pop turn envelopes and
    resolve the future from a canned reply map."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_result(reply_map.get(env.text, f"echo:{env.text}"))


class TestServeInbound:
    def test_post_v1_turn_roundtrip(self) -> None:
        """POST /v1/turn → consumer drains envelope → 200 with reply."""
        port = _free_port()
        replies = {"hello": "world"}

        async def _scenario() -> dict:
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map=replies))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            # Wait for the server to bind.
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("server never bound")

            def _do_post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/turn",
                    data=b'{"text": "hello"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    import json as _json

                    return resp.status, _json.loads(resp.read().decode())

            status, body = await asyncio.to_thread(_do_post)

            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return {"status": status, "body": body}

        result = asyncio.run(_scenario())
        assert result["status"] == 200
        assert result["body"]["reply"] == "world"
        assert result["body"]["exit_after"] is False

    def test_post_v1_turn_rejects_missing_text(self) -> None:
        """Empty body → 400."""
        port = _free_port()

        async def _scenario() -> int:
            import urllib.error
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map={}))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _do_post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/turn",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            code = await asyncio.to_thread(_do_post)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return code

        assert asyncio.run(_scenario()) == 400

    def test_health_endpoint(self) -> None:
        """GET /health → {status: ok}."""
        port = _free_port()

        async def _scenario() -> dict:
            import json as _json
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map={}))
            server = asyncio.create_task(
                serve_inbound(inbox, host="127.0.0.1", port=port, stop=stop)
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _get():
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=5.0
                ) as resp:
                    return _json.loads(resp.read().decode())

            body = await asyncio.to_thread(_get)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return body

        assert asyncio.run(_scenario()) == {"status": "ok"}


# ---------------------------------------------------------------------------
# /.well-known/agent-card.json — A2A AgentCard discovery
# ---------------------------------------------------------------------------


def _wait_bound(port: int) -> None:
    async def _wait():
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                await asyncio.sleep(0.05)
        pytest.fail("server never bound")

    asyncio.get_event_loop().run_until_complete(_wait())


class TestAgentCard:
    def _run_card_scenario(
        self, agent_name: str, yaml_path: str, url_suffix: str
    ) -> tuple[int, dict | None]:
        port = _free_port()

        async def _scenario() -> tuple[int, dict | None]:
            import json as _json
            import urllib.error
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map={}))
            server = asyncio.create_task(
                serve_inbound(
                    inbox,
                    host="127.0.0.1",
                    port=port,
                    stop=stop,
                    agent_name=agent_name,
                    spec_yaml_path=yaml_path,
                )
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _get():
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{url_suffix}", timeout=5.0
                    ) as resp:
                        return resp.status, _json.loads(resp.read().decode())
                except urllib.error.HTTPError as exc:
                    return exc.code, None

            status, body = await asyncio.to_thread(_get)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return status, body

        return asyncio.run(_scenario())

    def test_well_known_agent_card_returns_card(self, tmp_path) -> None:
        """GET /.well-known/agent-card.json → AgentCard from spec.yaml."""
        yaml_path = tmp_path / "ecosystem-auditor" / "spec.yaml"
        yaml_path.parent.mkdir()
        yaml_path.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata:\n"
            "  labels:\n"
            "    role: ecosystem-auditor\n"
            "    team: lab-a\n"
            "spec:\n"
            "  runtime: apptainer\n"
        )
        status, body = self._run_card_scenario(
            "ecosystem-auditor", str(yaml_path), "/.well-known/agent-card.json"
        )
        assert status == 200
        assert body is not None
        # Spec-required AgentCard fields per A2A.
        assert body["name"] == "ecosystem-auditor"
        assert "capabilities" in body
        assert "skills" in body
        # x-scitex-agent-container telemetry from the YAML labels.
        ext = body.get("x-scitex-agent-container", {})
        assert ext.get("role_class") == "ecosystem-auditor"

    def test_well_known_agent_card_404_without_yaml(self, tmp_path) -> None:
        """If the runner was launched without --a2a-card-yaml the
        endpoint returns 404 with a clear error body."""
        status, _ = self._run_card_scenario(
            "anything", "", "/.well-known/agent-card.json"
        )
        assert status == 404

    def test_well_known_agent_card_500_on_unreadable_yaml(self, tmp_path) -> None:
        """If the YAML path was passed but the file is missing, the
        endpoint surfaces a 500 rather than crashing the server."""
        missing = tmp_path / "nope" / "spec.yaml"
        status, _ = self._run_card_scenario(
            "anything", str(missing), "/.well-known/agent-card.json"
        )
        assert status == 500

    def test_card_url_uses_sac_listen_base_url_env(self, tmp_path, monkeypatch) -> None:
        """Layer 5: when ``SAC_LISTEN_BASE_URL`` is set, the card's
        ``url`` field uses that base — NOT the runner's volatile port.

        This is the contract that keeps an AgentCard's ``url`` stable
        across runner restarts under auto-port-allocation.
        """
        monkeypatch.setenv("SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878")
        yaml_path = tmp_path / "spec.yaml"
        yaml_path.write_text(
            "apiVersion: scitex-agent-container/v3\nkind: Agent\nspec:\n  runtime: apptainer\n"
        )
        status, body = self._run_card_scenario(
            "ecosystem-auditor", str(yaml_path), "/.well-known/agent-card.json"
        )
        assert status == 200
        assert body is not None
        # ADR-0004 — A2A v1 AgentCard: per-agent URL lives under
        # supportedInterfaces[0].url, not at the top level.
        assert (
            body["supportedInterfaces"][0]["url"]
            == "http://127.0.0.1:7878/agents/ecosystem-auditor"
        )

    def test_card_url_falls_back_to_request_base_when_env_unset(
        self, tmp_path, monkeypatch
    ) -> None:
        """Without ``SAC_LISTEN_BASE_URL`` the card's ``url`` falls
        back to ``request.base_url`` — keeps direct ``curl`` against
        the runner port working in non-apptainer test harnesses.
        """
        monkeypatch.delenv("SAC_LISTEN_BASE_URL", raising=False)
        yaml_path = tmp_path / "spec.yaml"
        yaml_path.write_text(
            "apiVersion: scitex-agent-container/v3\nkind: Agent\nspec:\n  runtime: apptainer\n"
        )
        status, body = self._run_card_scenario(
            "auditor", str(yaml_path), "/.well-known/agent-card.json"
        )
        assert status == 200
        assert body is not None
        # Without the env override the url is built from request.base_url.
        # ADR-0004 — A2A v1 AgentCard: URL is under supportedInterfaces[].
        per_agent_url = body["supportedInterfaces"][0]["url"]
        assert per_agent_url.startswith("http://127.0.0.1:")
        assert per_agent_url.endswith("/agents/auditor")


# ---------------------------------------------------------------------------
# Name-in-path routes — sidecar mirrors sac listen's shape
#
# The AgentCard advertises ``url: <base>/agents/<name>`` so a client
# POSTing to the discovered URL must succeed. Regression for that wart.
# ---------------------------------------------------------------------------


class TestNameInPathRoutes:
    def _post_turn(self, agent_name: str, path: str, text: str = "hi") -> int:
        """Spin up the sidecar with a fake consumer and POST to `path`.
        Returns the HTTP status code."""
        port = _free_port()
        replies = {text: "ack"}

        async def _scenario() -> int:
            import json as _json
            import urllib.error
            import urllib.request

            inbox = make_inbox()
            stop = asyncio.Event()
            consumer = asyncio.create_task(_fake_consumer(inbox, reply_map=replies))
            server = asyncio.create_task(
                serve_inbound(
                    inbox,
                    host="127.0.0.1",
                    port=port,
                    stop=stop,
                    agent_name=agent_name,
                )
            )
            for _ in range(50):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    await asyncio.sleep(0.05)

            def _post():
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=_json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        return resp.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            code = await asyncio.to_thread(_post)
            stop.set()
            await inbox.put(ShutdownEnvelope())
            await asyncio.wait_for(consumer, timeout=5.0)
            await asyncio.wait_for(server, timeout=5.0)
            return code

        return asyncio.run(_scenario())

    def test_canonical_sac_namespace_turn(self) -> None:
        """``POST /agents/<name>/turn`` — canonical sac path."""
        assert self._post_turn("alpha", "/agents/alpha/turn") == 200

    def test_canonical_sac_namespace_send(self) -> None:
        """``POST /agents/<name>/send`` — matches sac listen's verb."""
        assert self._post_turn("alpha", "/agents/alpha/send") == 200

    def test_name_mismatch_returns_404(self) -> None:
        """If the URL path's name doesn't match the agent on this port,
        return 404 with an explanatory body (sanity check — port
        routing already pinned us; the path name is informational)."""
        assert self._post_turn("alpha", "/agents/beta/turn") == 404

    def test_legacy_bare_turn_still_works(self) -> None:
        """``POST /v1/turn`` (the original shortcut) keeps working."""
        assert self._post_turn("alpha", "/v1/turn") == 200
