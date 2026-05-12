"""Tests for the peer-to-peer outbound client (Layer 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._network.peer import (
    PeerError,
    _read_yaml_endpoints,
    post_turn_to_url,
    resolve_peer_url,
)


class TestReadYamlEndpoints:
    def test_local_agent_with_a2a(self, tmp_path: Path) -> None:
        y = tmp_path / "a.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a:\n    port: 18888\n    host: 0.0.0.0\n"
        )
        assert _read_yaml_endpoints(str(y)) == ("0.0.0.0", 18888, None)

    def test_remote_dict_form(self, tmp_path: Path) -> None:
        y = tmp_path / "b.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 19000, host: 0.0.0.0}\n"
            "  remote: {host: mba, user: ywatanabe}\n"
        )
        assert _read_yaml_endpoints(str(y)) == ("0.0.0.0", 19000, "mba")

    def test_remote_string_form(self, tmp_path: Path) -> None:
        y = tmp_path / "c.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 20000}\n"
            "  remote: head-spartan\n"
        )
        assert _read_yaml_endpoints(str(y)) == (None, 20000, "head-spartan")

    def test_no_a2a_block(self, tmp_path: Path) -> None:
        y = tmp_path / "d.yaml"
        y.write_text("apiVersion: scitex-agent-container/v3\nkind: Agent\nspec: {}\n")
        assert _read_yaml_endpoints(str(y)) == (None, None, None)


class TestResolvePeerUrl:
    def test_local_agent_returns_loopback_url(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        agent_yaml = tmp_path / "alpha" / "alpha.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  a2a: {port: 18888}\n"
        )
        from scitex_agent_container.config import _resolve

        monkeypatch.setattr(_resolve, "resolve_config", lambda name: str(agent_yaml))
        # Re-import so peer.resolve_peer_url picks the patched lookup.
        from scitex_agent_container._network import peer as _peer

        # peer.resolve_peer_url imports lazily, so monkeypatch survives.
        url = _peer.resolve_peer_url("alpha")
        assert url == "http://127.0.0.1:18888/v1/turn"

    def test_remote_returns_ssh_scheme(self, tmp_path: Path, monkeypatch) -> None:
        """Remote agent → synthetic ssh:// URL; loopback a2a.host is fine
        because we tunnel through ssh."""
        agent_yaml = tmp_path / "beta" / "beta.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  a2a: {port: 19000}\n"
            "  remote: {host: mba}\n"
        )
        from scitex_agent_container.config import _resolve

        monkeypatch.setattr(_resolve, "resolve_config", lambda name: str(agent_yaml))
        url = resolve_peer_url("beta")
        assert url == "ssh://mba:19000/v1/turn"

    def test_no_a2a_port_raises(self, tmp_path: Path, monkeypatch) -> None:
        agent_yaml = tmp_path / "delta" / "delta.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n  runtime: apptainer\n"
        )
        from scitex_agent_container.config import _resolve

        monkeypatch.setattr(_resolve, "resolve_config", lambda name: str(agent_yaml))
        with pytest.raises(PeerError, match="no spec.a2a.port"):
            resolve_peer_url("delta")


class TestPostTurnToUrl:
    def test_url_must_end_in_v1_turn(self) -> None:
        with pytest.raises(PeerError, match="must end in /v1/turn"):
            post_turn_to_url("http://x:1/foo", "hi")

    def test_roundtrip_against_local_server(self) -> None:
        """Spin up a tiny http.server on 127.0.0.1 that mimics /v1/turn
        and assert post_turn_to_url returns the canned reply."""
        import http.server
        import json
        import socket
        import threading

        # Pick a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "reply": f"echo:{body.get('text', '')}",
                            "exit_after": body.get("exit_after", False),
                        }
                    ).encode("utf-8")
                )

            def log_message(self, *a, **kw):
                pass

        server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            reply = post_turn_to_url(
                f"http://127.0.0.1:{port}/v1/turn", "hello", timeout_s=5.0
            )
            assert reply == "echo:hello"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_unreachable_url_raises(self) -> None:
        # An unrouteable port → URLError → PeerError.
        with pytest.raises(PeerError):
            post_turn_to_url("http://127.0.0.1:1/v1/turn", "x", timeout_s=1.0)
