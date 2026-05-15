"""Layer-3 peer client: YAML endpoint parsing, URL resolution, HTTP POST.

Tests:
  1. ``_read_yaml_endpoints`` extracts (host, port, remote) from each YAML shape.
  2. ``resolve_peer_url`` returns loopback ``http://`` URL for local agents.
  3. ``resolve_peer_url`` returns synthetic ``ssh://`` URL for remote agents.
  4. ``resolve_peer_url`` raises ``PeerError`` when ``spec.a2a.port`` is absent.
  5. ``post_turn_to_url`` rejects URLs not ending in ``/v1/turn``.
  6. ``post_turn_to_url`` round-trips against a real local HTTP server.
  7. ``post_turn_to_url`` wraps transport failures in ``PeerError``.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).

PA-306: ``resolve_config`` is swapped via a ``_resolve_yaml`` pytest
fixture that does explicit save/restore on the real module attribute
— no ``monkeypatch.setattr``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._network.peer import (
    PeerError,
    _read_yaml_endpoints,
    post_turn_to_url,
    resolve_peer_url,
)


@pytest.fixture
def resolve_yaml_to():
    """Yield a setter that swaps ``resolve_config`` and restores on teardown."""
    from scitex_agent_container.config import _resolve

    saved = _resolve.resolve_config

    def _set(yaml_path):
        _resolve.resolve_config = lambda _name: str(yaml_path)

    try:
        yield _set
    finally:
        _resolve.resolve_config = saved


# ---------------------------------------------------------------------------
# 1 — _read_yaml_endpoints across YAML shapes
# ---------------------------------------------------------------------------


class TestReadYamlEndpoints:
    def test_local_agent_with_a2a_returns_host_port_and_none_remote(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        y = tmp_path / "a.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a:\n    port: 18888\n    host: 0.0.0.0\n"
        )
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == ("0.0.0.0", 18888, None)

    def test_remote_dict_form_extracts_remote_host(self, tmp_path: Path) -> None:
        # Arrange
        y = tmp_path / "b.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 19000, host: 0.0.0.0}\n"
            "  remote: {host: mba, user: ywatanabe}\n"
        )
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == ("0.0.0.0", 19000, "mba")

    def test_remote_string_form_used_directly_as_remote_host(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        y = tmp_path / "c.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 20000}\n"
            "  remote: head-spartan\n"
        )
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == (None, 20000, "head-spartan")

    def test_missing_a2a_block_returns_all_none(self, tmp_path: Path) -> None:
        # Arrange
        y = tmp_path / "d.yaml"
        y.write_text("apiVersion: scitex-agent-container/v3\nkind: Agent\nspec: {}\n")
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == (None, None, None)


# ---------------------------------------------------------------------------
# 2-4 — resolve_peer_url
# ---------------------------------------------------------------------------


class TestResolvePeerUrl:
    def test_local_agent_returns_loopback_http_url(
        self, tmp_path: Path, resolve_yaml_to
    ) -> None:
        # Arrange
        agent_yaml = tmp_path / "alpha" / "alpha.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  a2a: {port: 18888}\n"
        )
        resolve_yaml_to(agent_yaml)
        # Act
        url = resolve_peer_url("alpha")
        # Assert
        assert url == "http://127.0.0.1:18888/v1/turn"

    def test_remote_agent_returns_synthetic_ssh_scheme_url(
        self, tmp_path: Path, resolve_yaml_to
    ) -> None:
        # Arrange — loopback a2a.host is fine because we tunnel through ssh.
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
        resolve_yaml_to(agent_yaml)
        # Act
        url = resolve_peer_url("beta")
        # Assert
        assert url == "ssh://mba:19000/v1/turn"

    def test_missing_a2a_port_raises_peer_error_naming_field(
        self, tmp_path: Path, resolve_yaml_to
    ) -> None:
        # Arrange
        agent_yaml = tmp_path / "delta" / "delta.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n  runtime: apptainer\n"
        )
        resolve_yaml_to(agent_yaml)
        # Act
        action = lambda: resolve_peer_url("delta")
        # Assert
        with pytest.raises(PeerError, match="no spec.a2a.port"):
            action()


# ---------------------------------------------------------------------------
# 5-7 — post_turn_to_url
# ---------------------------------------------------------------------------


class TestPostTurnToUrl:
    def test_url_not_ending_in_v1_turn_raises_peer_error(self) -> None:
        # Arrange
        action = lambda: post_turn_to_url("http://x:1/foo", "hi")
        raised: list[BaseException] = []
        # Act
        try:
            action()
        except PeerError as exc:
            raised.append(exc)
        # Assert
        assert raised and "must end in /v1/turn" in str(raised[0])

    def test_roundtrip_against_local_server_returns_echoed_reply(self) -> None:
        """Spin up a tiny http.server on 127.0.0.1 that mimics /v1/turn
        and assert post_turn_to_url returns the canned reply."""
        # Arrange
        import http.server
        import json
        import socket
        import threading

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
            # Act
            reply = post_turn_to_url(
                f"http://127.0.0.1:{port}/v1/turn", "hello", timeout_s=5.0
            )
            # Assert
            assert reply == "echo:hello"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_unreachable_url_wraps_transport_failure_in_peer_error(self) -> None:
        # Arrange — port 1 on loopback is unrouteable → URLError → PeerError.
        action = lambda: post_turn_to_url(
            "http://127.0.0.1:1/v1/turn", "x", timeout_s=1.0
        )
        raised: list[BaseException] = []
        # Act
        try:
            action()
        except PeerError as exc:
            raised.append(exc)
        # Assert
        assert raised and isinstance(raised[0], PeerError)
