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

from scitex_agent_container._network.peer import (  # noqa: F401
    PeerError,
    _read_yaml_endpoints,
    post_turn,
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

    def test_spec_host_string_form_used_as_destination(self, tmp_path: Path) -> None:
        # Arrange — spec.host is the single source of truth for peer
        # destination (the same field cross-host dispatch consults).
        y = tmp_path / "b.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 19000, host: 0.0.0.0}\n"
            "  host: mba\n"
        )
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == ("0.0.0.0", 19000, "mba")

    def test_spec_host_alias_string_extracted_directly(self, tmp_path: Path) -> None:
        # Arrange
        y = tmp_path / "c.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 20000}\n"
            "  host: head-spartan\n"
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
            "  host: mba\n"
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
# 4b — resolve_peer_url cross-host fallback via the instances table.
#
# The remote-registry gap fix (sac-agent-spawn design, Rule B/F): an
# agent dispatched to another host claims its auto-port in THAT host's
# allocator, so the lead's local allocator has no claim. The cross-host
# dispatcher records the bound port + peer host in the lead's
# ``instances`` table; resolve_peer_url falls back to it so post-turn
# resolves a remote agent instead of raising "no bound port recorded".
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_db(tmp_path: Path):
    """Redirect state.db to a tmp path; reload the module so the
    module-level DEFAULT_DB_PATH picks it up (explicit save/restore)."""
    import importlib
    import os

    db = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(db)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield db
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _write_auto_port_yaml(tmp_path: Path) -> Path:
    """A v3 YAML with ``a2a.port: auto`` and no ``spec.host`` — the
    exact shape clew had (no static port, no local host pin)."""
    y = tmp_path / "clew" / "clew.yaml"
    y.parent.mkdir(parents=True)
    y.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  a2a: {port: auto}\n"
    )
    return y


class TestResolvePeerUrlCrossHostFallback:
    def test_remote_instances_row_resolves_to_ssh_url(
        self, pg_schema: str, tmp_path: Path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — auto-port YAML (no static port, no local allocator
        # claim) + a remote instances row recording the peer-resolved
        # bound port and host.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="spartan", bound_port=19123, remote=True
        )
        # Act
        url = resolve_peer_url("clew")
        # Assert — resolved to the recorded peer host + bound port.
        assert url == "ssh://spartan:19123/v1/turn"

    def test_remote_instances_row_without_local_claim_does_not_raise(
        self, pg_schema: str, tmp_path: Path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — same shape; the pre-fix behaviour was a PeerError
        # ("port: auto and no bound port recorded").
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="spartan", bound_port=19123, remote=True
        )
        raised: list[BaseException] = []
        # Act
        try:
            resolve_peer_url("clew")
        except PeerError as exc:
            raised.append(exc)
        # Assert
        assert raised == []

    def test_legacy_row_without_bound_port_falls_back_to_a2a_port(
        self, pg_schema: str, tmp_path: Path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — a row written before the family-tree columns existed
        # carries the port only in ``a2a_port``; the fallback must still
        # resolve it.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="spartan", a2a_port=19200, bound_port=None
        )
        # Act
        url = resolve_peer_url("clew")
        # Assert
        assert url == "ssh://spartan:19200/v1/turn"

    def test_no_instances_row_still_raises_auto_port_error(
        self, tmp_path: Path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — auto-port YAML, NO instances row, NO local claim:
        # the honest "is the agent running?" error must still fire.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import init_schema

        init_schema()
        # Act
        action = lambda: resolve_peer_url("clew")
        # Assert
        with pytest.raises(PeerError, match="no bound port recorded"):
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
                            "text": f"echo:{body.get('text', '')}",
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


# ---------------------------------------------------------------------------
# 8 — HTTP error-body propagation via real loopback server.
# ---------------------------------------------------------------------------


def _start_local_server(handler_cls):
    """Spin up a daemon BaseHTTPRequestHandler on 127.0.0.1; return (server, thread, port)."""
    import http.server
    import socket
    import threading

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _make_status_handler(status: int, body: bytes, content_type: str = "text/plain"):
    import http.server

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):
            pass

    return _Handler


class TestPostTurnHttpErrorPaths:
    def test_http_500_response_raises_peer_error(self) -> None:
        # Arrange
        handler = _make_status_handler(500, b"boom-detail")
        server, thread, port = _start_local_server(handler)
        try:
            # Act
            action = lambda: post_turn_to_url(
                f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
            )
            # Assert
            with pytest.raises(PeerError, match="HTTP 500"):
                action()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_http_error_includes_response_body(self) -> None:
        # Arrange
        handler = _make_status_handler(503, b"upstream-down")
        server, thread, port = _start_local_server(handler)
        raised: list[BaseException] = []
        try:
            # Act
            try:
                post_turn_to_url(
                    f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
                )
            except PeerError as exc:
                raised.append(exc)
            # Assert
            assert raised and "upstream-down" in str(raised[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_malformed_json_reply_raises_peer_error(self) -> None:
        # Arrange
        handler = _make_status_handler(
            200, b'{"not_reply": "oops"}', "application/json"
        )
        server, thread, port = _start_local_server(handler)
        try:
            # Act
            action = lambda: post_turn_to_url(
                f"http://127.0.0.1:{port}/v1/turn", "hi", timeout_s=5.0
            )
            # Assert
            with pytest.raises(PeerError, match="malformed body"):
                action()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 9 — resolve_peer_url: FileNotFoundError → PeerError; post_turn end-to-end.
# ---------------------------------------------------------------------------


class TestResolvePeerUrlMissingYaml:
    def test_missing_yaml_raises_peer_error(self) -> None:
        # Arrange
        from scitex_agent_container.config import _resolve

        saved = _resolve.resolve_config

        def _raiser(_name):
            raise FileNotFoundError("agent 'ghost' not found in any search path")

        _resolve.resolve_config = _raiser
        try:
            # Act
            action = lambda: resolve_peer_url("ghost")
            # Assert
            with pytest.raises(PeerError, match="ghost"):
                action()
        finally:
            _resolve.resolve_config = saved


class TestPostTurnEndToEnd:
    def test_post_turn_resolves_yaml_and_round_trips_reply(
        self, tmp_path: Path, resolve_yaml_to
    ) -> None:
        # Arrange — real local HTTP server + real YAML resolution.
        import http.server
        import json as _json

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = _json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    _json.dumps({"text": f"ack:{body['text']}"}).encode("utf-8")
                )

            def log_message(self, *a, **kw):
                pass

        server, thread, port = _start_local_server(_Handler)
        agent_yaml = tmp_path / "gamma" / "gamma.yaml"
        agent_yaml.parent.mkdir(parents=True)
        agent_yaml.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            f"  a2a: {{port: {port}}}\n"
        )
        resolve_yaml_to(agent_yaml)
        try:
            # Act
            reply = post_turn("gamma", "ping", timeout_s=5.0)
            # Assert
            assert reply == "ack:ping"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 10 — SSH transport fallback via PATH-installed ssh shim.
# ---------------------------------------------------------------------------


class TestPostTurnViaSsh:
    def test_ssh_fallback_returns_reply_from_remote_curl(self, subprocess_shim) -> None:
        # Arrange — fake ssh prints a JSON envelope to stdout.
        import json as _json

        stdout = _json.dumps({"text": "remote-ack"}) + "\n"
        subprocess_shim.install("ssh", stdout=stdout, exit=0)
        # Act
        reply = post_turn_to_url("ssh://mba:18888/v1/turn", "hi", timeout_s=2.0)
        # Assert
        assert reply == "remote-ack"

    def test_ssh_fallback_invokes_ssh_with_target_host(self, subprocess_shim) -> None:
        # Arrange
        import json as _json

        stdout = _json.dumps({"text": "ok"}) + "\n"
        subprocess_shim.install("ssh", stdout=stdout, exit=0)
        # Act
        post_turn_to_url("ssh://my-host:19000/v1/turn", "hi", timeout_s=2.0)
        # Assert
        argv = subprocess_shim.argv_for("ssh")
        assert "my-host" in argv

    def test_ssh_fallback_skips_banner_lines_before_json(self, subprocess_shim) -> None:
        # Arrange — .bashrc banner precedes the JSON body.
        import json as _json

        stdout = (
            "Welcome to remote host\n"
            "Last login: yesterday\n" + _json.dumps({"text": "after-banner"}) + "\n"
        )
        subprocess_shim.install("ssh", stdout=stdout, exit=0)
        # Act
        reply = post_turn_to_url("ssh://mba:18888/v1/turn", "hi", timeout_s=2.0)
        # Assert
        assert reply == "after-banner"

    def test_ssh_nonzero_exit_raises_peer_error_with_stderr(
        self, subprocess_shim
    ) -> None:
        # Arrange
        subprocess_shim.install("ssh", stdout="", stderr="permission denied", exit=255)
        # Act
        action = lambda: post_turn_to_url(
            "ssh://mba:18888/v1/turn", "hi", timeout_s=2.0
        )
        # Assert
        with pytest.raises(PeerError, match="permission denied"):
            action()

    def test_ssh_non_json_stdout_raises_peer_error(self, subprocess_shim) -> None:
        # Arrange
        subprocess_shim.install("ssh", stdout="not json at all\n", exit=0)
        # Act
        action = lambda: post_turn_to_url(
            "ssh://mba:18888/v1/turn", "hi", timeout_s=2.0
        )
        # Assert
        with pytest.raises(PeerError, match="non-JSON"):
            action()

    def test_ssh_empty_stdout_raises_peer_error(self, subprocess_shim) -> None:
        # Arrange
        subprocess_shim.install("ssh", stdout="", exit=0)
        # Act
        action = lambda: post_turn_to_url(
            "ssh://mba:18888/v1/turn", "hi", timeout_s=2.0
        )
        # Assert
        with pytest.raises(PeerError, match="non-JSON"):
            action()

    def test_ssh_reply_missing_key_raises_malformed_body(self, subprocess_shim) -> None:
        # Arrange
        import json as _json

        subprocess_shim.install("ssh", stdout=_json.dumps({"other": 1}) + "\n", exit=0)
        # Act
        action = lambda: post_turn_to_url(
            "ssh://mba:18888/v1/turn", "hi", timeout_s=2.0
        )
        # Assert
        with pytest.raises(PeerError, match="malformed body"):
            action()

    def test_ssh_url_without_host_raises_peer_error(self) -> None:
        # Arrange
        bad_url = "ssh:///v1/turn"
        # Act
        action = lambda: post_turn_to_url(bad_url, "hi", timeout_s=1.0)
        # Assert
        with pytest.raises(PeerError, match="malformed ssh URL"):
            action()


# ---------------------------------------------------------------------------
# 11 — _read_yaml_endpoints extra shapes: remote list, unreadable file.
# ---------------------------------------------------------------------------


class TestReadYamlEndpointsExtras:
    def test_spec_host_list_form_uses_last_alias(self, tmp_path: Path) -> None:
        # Arrange — chain form: priority list of hosts, route to the last.
        y = tmp_path / "chain.yaml"
        y.write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  a2a: {port: 22000}\n"
            "  host: [bastion, intermediate, final-host]\n"
        )
        # Act
        result = _read_yaml_endpoints(str(y))
        # Assert
        assert result == (None, 22000, "final-host")

    def test_unreadable_yaml_returns_all_none(self, tmp_path: Path) -> None:
        # Arrange — pointer to a path that does not exist.
        missing = tmp_path / "nope.yaml"
        # Act
        result = _read_yaml_endpoints(str(missing))
        # Assert
        assert result == (None, None, None)
