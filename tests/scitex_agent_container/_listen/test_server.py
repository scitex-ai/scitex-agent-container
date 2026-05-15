"""HTTP-route coverage for ``_listen.server`` (PA-306 no-mocks).

Drives the real Starlette app through :class:`starlette.testclient.TestClient`
with real fixture files under ``tmp_path`` and real bearer-token auth.
Module-level path roots (registry dir, runtime dir, ``$HOME``) are
re-pointed at the per-test tmp directory by assigning to the module
attributes — that's configuration, not mocking.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg

TOKEN = "test-token-abc123"


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Point every on-disk root at ``tmp_path`` for the duration of one test."""
    saved_home = os.environ.get("HOME")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_yaml_env = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"

    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        for key, val in (
            ("HOME", saved_home),
            ("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", saved_reg_env),
            ("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", saved_run_env),
            ("SCITEX_AGENT_CONTAINER_YAML_DIRS", saved_yaml_env),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


@pytest.fixture
def client(isolated_env):
    """Real Starlette TestClient against ``create_app`` with bearer auth."""
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_spec(home: Path, name: str, workdir: str = "/tmp") -> Path:
    """Write a minimal v3 spec.yaml that ``load_config`` will accept."""
    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "claude": {"model": "claude-sonnet-4-5"},
            "workdir": workdir,
        },
    }
    p = spec_dir / "spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


# --- Health (public) ------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_ok_true(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.json()["ok"] is True

    def test_health_works_without_auth(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.status_code == 200

    def test_health_advertises_service_name(self, client):
        # Arrange
        url = "/v1/health"
        # Act
        body = client.get(url).json()
        # Assert
        assert body["service"] == "sac-listen"


# --- Auth middleware ------------------------------------------------------


class TestBearerAuthMiddleware:
    def test_missing_token_returns_401(self, client):
        # Arrange
        url = "/agents"
        # Act
        resp = client.get(url)
        # Assert
        assert resp.status_code == 401

    def test_missing_token_error_body_explains(self, client):
        # Arrange
        url = "/agents"
        # Act
        body = client.get(url).json()
        # Assert
        assert "missing bearer token" in body["error"]

    def test_wrong_token_returns_403(self, client):
        # Arrange
        headers = {"authorization": "Bearer nope"}
        # Act
        resp = client.get("/agents", headers=headers)
        # Assert
        assert resp.status_code == 403

    def test_wrong_token_error_body_explains(self, client):
        # Arrange
        headers = {"authorization": "Bearer nope"}
        # Act
        body = client.get("/agents", headers=headers).json()
        # Assert
        assert "invalid bearer token" in body["error"]

    def test_malformed_authorization_header_returns_401(self, client):
        # Arrange
        headers = {"authorization": "Basic xyz"}
        # Act
        resp = client.get("/agents", headers=headers)
        # Assert
        assert resp.status_code == 401

    def test_valid_token_passes_through(self, client, auth_headers):
        # Arrange
        url = "/agents"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 200


# --- GET /agents (list) ---------------------------------------------------


class TestListAgents:
    def test_empty_registry_returns_empty_list(self, client, auth_headers):
        # Arrange
        url = "/agents"
        # Act
        body = client.get(url, headers=auth_headers).json()
        # Assert
        assert body == {"agents": []}

    def test_registered_agent_appears_in_list(self, client, auth_headers, isolated_env):
        # Arrange
        r = _reg.Registry()
        r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
        # Act
        body = client.get("/agents", headers=auth_headers).json()
        # Assert
        assert body["agents"][0]["name"] == "alpha"


# --- GET /agents/<name>/status -------------------------------------------


class TestAgentStatus:
    def test_unknown_agent_returns_404(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/status"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_unknown_agent_body_has_error(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/status"
        # Act
        body = client.get(url, headers=auth_headers).json()
        # Assert
        assert "error" in body

    def test_known_agent_returns_name(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert body["name"] == "beta"

    def test_known_agent_returns_state_dir(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert "beta" in body["state_dir"]


# --- POST /agents/<name>/send --------------------------------------------


class TestAgentSendValidation:
    def test_non_json_body_returns_400(self, client, auth_headers):
        # Arrange
        headers = {**auth_headers, "content-type": "application/json"}
        # Act
        resp = client.post("/agents/x/send", content=b"not-json", headers=headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_type_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "weird"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_missing_prompt_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_empty_prompt_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt", "prompt": ""}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_agent_for_prompt_returns_404(self, client, auth_headers):
        # Arrange
        body = {"type": "prompt", "prompt": "hi"}
        # Act
        resp = client.post("/agents/no-such/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_prompt_without_session_id_returns_409(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        _write_spec(isolated_env, "gamma")
        # Act
        resp = client.post(
            "/agents/gamma/send",
            json={"type": "prompt", "prompt": "hello"},
            headers=auth_headers,
        )
        # Assert
        assert resp.status_code == 409


class TestAgentSendKey:
    def test_unknown_key_returns_400(self, client, auth_headers):
        # Arrange
        body = {"type": "key", "key": "F13"}
        # Act
        resp = client.post("/agents/x/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_unknown_key_error_lists_supported(self, client, auth_headers):
        # Arrange
        body = {"type": "key", "key": "F13"}
        # Act
        out = client.post("/agents/x/send", json=body, headers=auth_headers).json()
        # Assert
        assert "ESC" in out["error"]

    def test_key_without_pid_file_returns_404(self, client, auth_headers, isolated_env):
        # Arrange
        body = {"type": "key", "key": "ESC"}
        # Act
        resp = client.post("/agents/ghost/send", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_key_with_pid_file_signals_running_process(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        import signal

        prev = signal.signal(signal.SIGINT, lambda *_: None)
        state_dir = _ss.state_dir_for("delta")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pid").write_text(str(os.getpid()))
        # Act
        try:
            resp = client.post(
                "/agents/delta/send",
                json={"type": "key", "key": "SIGINT"},
                headers=auth_headers,
            )
        finally:
            signal.signal(signal.SIGINT, prev)
        # Assert
        assert resp.status_code == 200

    def test_key_with_malformed_pid_returns_500(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        state_dir = _ss.state_dir_for("epsilon")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "pid").write_text("not-a-pid")
        # Act
        resp = client.post(
            "/agents/epsilon/send",
            json={"type": "key", "key": "ESC"},
            headers=auth_headers,
        )
        # Assert
        assert resp.status_code == 500


# --- POST /agents (start) -------------------------------------------------


class TestAgentsStartValidation:
    def test_non_json_body_returns_400(self, client, auth_headers):
        # Arrange
        headers = {**auth_headers, "content-type": "application/json"}
        # Act
        resp = client.post("/agents", content=b"xxx", headers=headers)
        # Assert
        assert resp.status_code == 400

    def test_missing_name_returns_400(self, client, auth_headers):
        # Arrange
        body: dict = {}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_empty_name_returns_400(self, client, auth_headers):
        # Arrange
        body = {"name": ""}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_non_dict_returns_400(self, client, auth_headers):
        # Arrange
        body = {"name": "z", "spec": "not-a-dict"}
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_wrong_api_version_returns_400(self, client, auth_headers):
        # Arrange
        body = {
            "name": "z",
            "spec": {"apiVersion": "wrong", "kind": "Agent"},
        }
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400

    def test_inline_spec_wrong_kind_returns_400(self, client, auth_headers):
        # Arrange
        body = {
            "name": "z",
            "spec": {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Other",
            },
        }
        # Act
        resp = client.post("/agents", json=body, headers=auth_headers)
        # Assert
        assert resp.status_code == 400


# --- GET /agents/<name>/card ---------------------------------------------


class TestAgentCard:
    def test_unknown_agent_returns_404(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/card"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_known_agent_card_returns_200(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        resp = client.get("/agents/zeta/card", headers=auth_headers)
        # Assert
        assert resp.status_code == 200

    def test_known_agent_card_body_is_dict(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        body = client.get("/agents/zeta/card", headers=auth_headers).json()
        # Assert
        assert isinstance(body, dict)


# --- DELETE /agents/<name> -----------------------------------------------


class TestAgentDelete:
    def test_no_pid_file_returns_404(self, client, auth_headers, isolated_env):
        # Arrange
        url = "/agents/no-such"
        # Act
        resp = client.delete(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_malformed_pid_returns_500(self, client, auth_headers, isolated_env):
        # Arrange
        sd = _ss.state_dir_for("bad")
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "pid").write_text("not-an-int")
        # Act
        resp = client.delete("/agents/bad", headers=auth_headers)
        # Assert
        assert resp.status_code == 500

    def test_live_pid_returns_stopped_true(self, client, auth_headers, isolated_env):
        # Arrange
        import signal

        prev = signal.signal(signal.SIGTERM, lambda *_: None)
        sd = _ss.state_dir_for("livepid")
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "pid").write_text(str(os.getpid()))
        # Act
        try:
            body = client.delete("/agents/livepid", headers=auth_headers).json()
        finally:
            signal.signal(signal.SIGTERM, prev)
        # Assert
        assert body["stopped"] is True


# --- GET /agents/<name>/tail (SSE) ---------------------------------------


class TestAgentTail:
    def test_missing_session_jsonl_no_follow_returns_404(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        url = "/agents/missing/tail?follow=false"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_tail_streams_recorded_lines(self, client, auth_headers, isolated_env):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text(
            '{"ts": "2025-01-01T00:00:00", "msg": "hello"}\n'
            '{"ts": "2025-01-01T00:00:01", "msg": "world"}\n',
            encoding="utf-8",
        )
        # Act
        resp = client.get("/agents/tailer/tail?follow=false", headers=auth_headers)
        # Assert
        assert "hello" in resp.text

    def test_tail_content_type_is_event_stream(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer2"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text('{"msg": "x"}\n', encoding="utf-8")
        # Act
        resp = client.get("/agents/tailer2/tail?follow=false", headers=auth_headers)
        # Assert
        assert "text/event-stream" in resp.headers["content-type"]

    def test_tail_with_since_filters_old_records(
        self, client, auth_headers, isolated_env
    ):
        # Arrange
        rt = isolated_env / ".scitex" / "agent-container" / "runtime" / "tailer3"
        rt.mkdir(parents=True, exist_ok=True)
        (rt / "session.jsonl").write_text(
            '{"ts": "2020-01-01T00:00:00", "msg": "old"}\n'
            '{"ts": "2030-01-01T00:00:00", "msg": "future"}\n',
            encoding="utf-8",
        )
        # Act
        resp = client.get(
            "/agents/tailer3/tail?follow=false&since=2025-01-01T00:00:00",
            headers=auth_headers,
        )
        # Assert
        assert "old" not in resp.text


# --- Unknown route --------------------------------------------------------


class TestUnknownRoute:
    def test_unknown_path_returns_404(self, client, auth_headers):
        # Arrange
        url = "/v1/nope"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404
