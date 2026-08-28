"""HTTP-route coverage for ``_listen.server`` (PA-306 no-mocks).

Drives the real Starlette app through :class:`starlette.testclient.TestClient`
with real fixture files under ``tmp_path`` and real bearer-token auth.
Module-level path roots (registry dir, runtime dir, ``$HOME``) are
re-pointed at the per-test tmp directory by assigning to the module
attributes — that's configuration, not mocking.

The WI-4 cross-host forwarder tests live at the bottom of this
file. The forwarder helper (``_forward_to_remote``) and the
``node_message_send`` route that invokes it both live in
``_listen/server.py``, so this is the canonical mirror per the
PS-204 orphan-test-file rule. The cross-host section uses its own
``cross_host_env`` fixture (broader scope: state.db + peer-tokens
registry) and a distinct ``SHARED_TOKEN`` constant so the
WI-4 loopback tests do not collide with the existing route tests'
``isolated_env`` / ``TOKEN``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state import state_db_nodes as state_db_nodes_grant
from scitex_agent_container._state.state_db_nodes import record_lineage
from tests.scitex_agent_container._helpers.loopback_server import run_loopback

TOKEN = "test-token-abc123"

# WI-4 cross-host forwarder: both apps in the loopback tests run
# with the same listen token — Q4(b) per-host bearer registry: the
# forwarder pulls ``peer-tokens/host-a.token`` (= this value) when
# forwarding to host A. Real deployments mint independent per-host
# tokens.
SHARED_TOKEN = "test-token-wi4"


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path):
    """Point every on-disk root at ``tmp_path`` for the duration of one test.

    The self-peer discovery (op-2026-06-12-15) calls
    :func:`config._resolve._project_local_dirs` which walks CWD
    upward for ``.scitex/agent-container/agents/`` — the sac repo's
    own checked-in ``agents/self/spec.yaml`` would otherwise leak
    into every ``list_agents`` response. Stub that helper to ``[]``
    for the test's lifetime so the only sources are the tmp_path-
    rooted registry + ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (popped).
    """
    from scitex_agent_container.config import _resolve as _config_resolve

    saved_home = os.environ.get("HOME")
    saved_reg_env = os.environ.get("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    saved_run_env = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    saved_yaml_env = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_project_local = _config_resolve._project_local_dirs

    os.environ["HOME"] = str(tmp_path)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(tmp_path / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(tmp_path / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    _config_resolve._project_local_dirs = lambda start=None: []

    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        _config_resolve._project_local_dirs = saved_project_local
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
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    # Red-start ruling 2026-07-21: every field explicit (curated wins).
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": explicit_spec(
            {
                "runtime": "tui",
                "host": "${HOSTNAME}",
                "workdir": workdir,
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "claude-sonnet-4-5"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            }
        ),
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
        # Arrange — the self-peer + comms-node auto-registration feature
        # (origin/feat/self-peer-self-register, merged 2026-06-13) means
        # /agents always surfaces the self-peer row even when no MANAGED
        # agents have been registered. Filter comms-node rows to recover
        # the original "empty managed registry" assertion.
        url = "/agents"
        # Act
        body = client.get(url, headers=auth_headers).json()
        managed = [a for a in body["agents"] if a.get("kind") != "comms-node"]
        # Assert
        assert managed == []

    def test_registered_agent_appears_in_list(self, client, auth_headers, isolated_env):
        # Arrange
        r = _reg.Registry()
        r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
        # Act
        body = client.get("/agents", headers=auth_headers).json()
        # Assert
        assert body["agents"][0]["name"] == "alpha"

    def test_registered_agent_row_carries_a2a_port_when_allocator_claimed(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: when port_allocator has a claim, the row carries it.
        from scitex_agent_container._state import port_allocator as _pa
        from scitex_agent_container._state import state_db as _state_db

        db = isolated_env / "state.db"
        os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
        saved_db_path = _state_db.DEFAULT_DB_PATH
        _state_db.DEFAULT_DB_PATH = db
        try:
            _state_db.init_schema(db)
            _pa.claim_port("alpha", range_=(22000, 22001), db_path=db)
            r = _reg.Registry()
            r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
            # Act
            body = client.get("/agents", headers=auth_headers).json()
            # Assert
            assert body["agents"][0]["a2a_port"] == 22000
        finally:
            _state_db.DEFAULT_DB_PATH = saved_db_path
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)

    def test_registered_agent_row_carries_turn_url_when_allocator_claimed(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: turn_url ships alongside a2a_port for the
        # nudge→turn dispatcher.
        from scitex_agent_container._state import port_allocator as _pa
        from scitex_agent_container._state import state_db as _state_db

        db = isolated_env / "state.db"
        os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
        saved_db_path = _state_db.DEFAULT_DB_PATH
        _state_db.DEFAULT_DB_PATH = db
        try:
            _state_db.init_schema(db)
            _pa.claim_port("alpha", range_=(22100, 22101), db_path=db)
            r = _reg.Registry()
            r.add("alpha", "/path/to/spec.yaml", "sc-alpha", pid=12345)
            # Act
            body = client.get("/agents", headers=auth_headers).json()
            # Assert
            assert body["agents"][0]["turn_url"].endswith(":22100/v1/turn")
        finally:
            _state_db.DEFAULT_DB_PATH = saved_db_path
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)

    def test_registered_agent_row_carries_null_a2a_port_when_no_claim(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: when port_allocator has NO claim for the name,
        # the row still ships the key (= null) so consumers can branch
        # on field presence rather than key presence.
        from scitex_agent_container._state import state_db as _state_db

        db = isolated_env / "state.db"
        os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
        saved_db_path = _state_db.DEFAULT_DB_PATH
        _state_db.DEFAULT_DB_PATH = db
        try:
            _state_db.init_schema(db)
            r = _reg.Registry()
            r.add("portless", "/path/to/spec.yaml", "sc-portless", pid=98765)
            # Act
            body = client.get("/agents", headers=auth_headers).json()
            # Assert
            assert body["agents"][0]["a2a_port"] is None
        finally:
            _state_db.DEFAULT_DB_PATH = saved_db_path
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)

    def test_registered_agent_row_carries_null_turn_url_when_no_claim(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: turn_url is null when a2a_port resolves to null
        # (derive_turn_url's bothness rule).
        from scitex_agent_container._state import state_db as _state_db

        db = isolated_env / "state.db"
        os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
        saved_db_path = _state_db.DEFAULT_DB_PATH
        _state_db.DEFAULT_DB_PATH = db
        try:
            _state_db.init_schema(db)
            r = _reg.Registry()
            r.add("portless", "/path/to/spec.yaml", "sc-portless", pid=98765)
            # Act
            body = client.get("/agents", headers=auth_headers).json()
            # Assert
            assert body["agents"][0]["turn_url"] is None
        finally:
            _state_db.DEFAULT_DB_PATH = saved_db_path
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)


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

    def test_known_agent_status_carries_a2a_port_key(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: ``a2a_port`` ships even when no claim exists
        # (consumers branch on field value, not key presence).
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert "a2a_port" in body

    def test_known_agent_status_carries_turn_url_key(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: ``turn_url`` ships alongside ``a2a_port``.
        _write_spec(isolated_env, "beta")
        # Act
        body = client.get("/agents/beta/status", headers=auth_headers).json()
        # Assert
        assert "turn_url" in body

    def test_known_agent_status_carries_resolved_turn_url_when_port_claimed(
        self, client, auth_headers, isolated_env
    ):
        # Arrange — Q1: when port_allocator has a claim, status surfaces
        # the derived turn_url so scitex-todo's resolver can dispatch.
        from scitex_agent_container._state import port_allocator as _pa
        from scitex_agent_container._state import state_db as _state_db

        db = isolated_env / "state.db"
        os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
        saved_db_path = _state_db.DEFAULT_DB_PATH
        _state_db.DEFAULT_DB_PATH = db
        try:
            _state_db.init_schema(db)
            _write_spec(isolated_env, "beta")
            _pa.claim_port("beta", range_=(23000, 23001), db_path=db)
            # Act
            body = client.get("/agents/beta/status", headers=auth_headers).json()
            # Assert
            assert body["turn_url"].endswith(":23000/v1/turn")
        finally:
            _state_db.DEFAULT_DB_PATH = saved_db_path
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)


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


# --- GET /agents/<name>/.well-known/agent-card.json ----------------------


class TestAgentCard:
    def test_unknown_agent_returns_404(self, client, auth_headers):
        # Arrange
        url = "/agents/no-such/.well-known/agent-card.json"
        # Act
        resp = client.get(url, headers=auth_headers)
        # Assert
        assert resp.status_code == 404

    def test_known_agent_card_returns_200(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        resp = client.get(
            "/agents/zeta/.well-known/agent-card.json", headers=auth_headers
        )
        # Assert
        assert resp.status_code == 200

    def test_known_agent_card_body_is_dict(self, client, auth_headers, isolated_env):
        # Arrange
        _write_spec(isolated_env, "zeta")
        # Act
        body = client.get(
            "/agents/zeta/.well-known/agent-card.json", headers=auth_headers
        ).json()
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


# ---------------------------------------------------------------------------
# WI-4 — cross-host forwarder on ``sac listen`` (handoff §4).
#
# Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4):
#
#   Acceptance: a node on host B sends to one on host A; the event
#   arrives, ACL-checked at A.
#
# The end-to-end tests drive two real ``uvicorn`` instances on
# loopback ports to simulate the two-host topology:
#
#   * "host A" — owns the target. ACL is gated here.
#   * "host B" — the forwarding entry point. Records the target's
#     instance under ``host="host-a"`` so the resolver routes there.
#
# A POST to host B's ``message:send`` for that target arrives on
# host A's broker. The Q4(b) per-host bearer registry is exercised
# by the missing-peer-token loud-502 tests at the bottom.
#
# No mocks (handoff §0): real SQLite + real ``uvicorn``.
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_env(tmp_path: Path):
    """Isolated state.db + runtime/registry roots + peer-token for host A.

    Broader scope than ``isolated_env`` above (which only redirects
    HOME / registry / runtime): this fixture also wires up state.db
    and seeds the WI-4 Q4(b) per-host bearer registry under
    ``~/.scitex/agent-container/peer-tokens/`` so the forwarder can
    authenticate at host A.
    """
    # Arrange
    db = tmp_path / "state.db"
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT

    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    # WI-4 Q4(b): seed the per-host bearer registry. The forwarder
    # pulls ``peer-tokens/host-a.token`` to authenticate when
    # forwarding to host A. ``$HOME`` is already tmp_path so the
    # default ``~/.scitex/agent-container/peer-tokens/`` lands here.
    write_peer_token(peer_host="host-a", token=SHARED_TOKEN)
    try:
        yield {"db": db, "tmp": tmp_path}
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Ceiling for the REAL cross-host I/O below (loopback HTTP + the SSE roundtrip
# between two live uvicorn servers). Generous ON PURPOSE, and not a magic number:
# every wait it bounds is event-driven — the httpx read returns the instant the
# forwarded event lands, the `wait_for`s the instant their event fires — so a
# large ceiling costs NOTHING on the happy path and only bounds a genuinely
# broken run. The 5.0s literals that used to be sprinkled here were the second
# half of the py3.11 CI flake: once the `uvicorn loopback did not start in 5s`
# ceiling was fixed, a loaded runner simply failed one layer down instead, with
# `httpx.ReadTimeout` (reproduced: 6 cross-host tests fail under CPU
# oversubscription purely on these 5s bounds). Bounding real I/O by an arbitrary
# tight deadline is a race by construction; the fix is to make the bound
# generous, not to guess a better number.
LOOPBACK_IO_TIMEOUT_S = 30.0


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin up uvicorn on a loopback port. The app's
    ``local_host`` identity is configured at ``create_app`` time
    (see :func:`scitex_agent_container._listen.server.create_app`).

    Startup wait lives in the shared helper — the hand-rolled 5s ceiling this
    used to carry raced the listen lifespan (measured 7.49s under load) and
    turned the py3.11 leg red. See ``_helpers/loopback_server.py``.
    """
    with run_loopback(app, port) as p:
        yield p


def _send_payload(text: str, *, from_agent: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": {"from_agent": from_agent},
        },
    }


def test_cross_host_send_forwards_to_target_host(cross_host_env, pg_schema: str) -> None:
    """End-to-end: a POST to host B's ``message:send`` for a target
    pinned to host A arrives on host A's broker.
    """
    # Arrange
    db = cross_host_env["db"]
    # Register the target as a live instance on host-a.
    state_db.record_instance_start(name="alice", host="host-a", a2a_port=0, db_path=db)
    # Permitted-peer is registered as a child of root, so is alice;
    # they share a group and ACL allows the send.
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)

    host_a_port = _free_port()
    host_b_port = _free_port()

    # Bind the actual port for host A onto the instances row so the
    # resolver routes to the right loopback.
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )

    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            # Subscribe on host A as alice.
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=LOOPBACK_IO_TIMEOUT_S)
                # Now stand up host B and post to it. WI-4 forwarder
                # on host B should resolve alice→host-a and forward.
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                        resp = await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(
                                "hi from b", from_agent="permitted-peer"
                            ),
                            headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                        )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"forward returned {resp.status_code}: {resp.text!r}"
                    )
                await asyncio.wait_for(sub, timeout=LOOPBACK_IO_TIMEOUT_S)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    # Act
    event = asyncio.run(driver())
    # Assert
    assert event.get("content") == "hi from b"


def test_cross_host_forward_preserves_from_agent_metadata(cross_host_env, pg_schema: str) -> None:
    """The forwarded event keeps the original ``from_agent`` so
    host A's ACL can gate on the real sender, not the forwarding
    host's identity.
    """
    # Arrange
    db = cross_host_env["db"]
    state_db.record_instance_start(name="alice", host="host-a", a2a_port=0, db_path=db)
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    host_a_port = _free_port()
    host_b_port = _free_port()
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )
    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=LOOPBACK_IO_TIMEOUT_S)
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                        await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload("x", from_agent="permitted-peer"),
                            headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                        )
                await asyncio.wait_for(sub, timeout=LOOPBACK_IO_TIMEOUT_S)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    # Act
    event = asyncio.run(driver())
    # Assert
    assert event.get("from_agent") == "permitted-peer"


# ---------------------------------------------------------------------------
# WI-4 Q4(b): missing peer-token → loud 502 (handoff §0 — no silent drop).
# ---------------------------------------------------------------------------


@pytest.fixture
def missing_peer_token_response(tmp_path: Path):
    """Drive a forwarder POST with NO peer-token written for the
    destination host, so the forwarder must fall through to the loud
    502 path. Yielded value is the live ``httpx.Response`` so each
    test can assert one aspect of the failure shape.
    """
    # Arrange — fresh tmp env, NO peer-token for host-z.
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_db_const = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    db = tmp_path / "state.db"
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    state_db.init_schema(db)
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    state_db.record_instance_start(
        name="alice", host="host-z", a2a_port=9999, db_path=db
    )
    app_local = create_app(token=SHARED_TOKEN, local_host="host-b")

    try:
        with TestClient(app_local) as client:
            r = client.post(
                "/agents/alice/message:send",
                json=_send_payload("hi", from_agent="permitted-peer"),
                headers={"authorization": f"Bearer {SHARED_TOKEN}"},
            )
        yield r
    finally:
        state_db.DEFAULT_DB_PATH = saved_db_const
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_db_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def test_cross_host_forward_502_when_peer_token_missing(
    missing_peer_token_response,
) -> None:
    """When forwarding to a host whose bearer is NOT in
    ``peer-tokens/``, the forwarder fails loudly with 502
    (handoff §0 — no silent drop).
    """
    # Arrange
    r = missing_peer_token_response
    # Act
    status = r.status_code
    # Assert
    assert status == 502, r.text


def test_cross_host_forward_502_body_names_missing_host(
    missing_peer_token_response,
) -> None:
    """The 502 body names the specific peer host so the operator
    sees which ``sac host add-peer`` to run."""
    # Arrange
    r = missing_peer_token_response
    # Act
    err = r.json().get("error", "")
    # Assert
    assert "host-z" in err


def test_cross_host_forward_502_body_carries_add_peer_fix(
    missing_peer_token_response,
) -> None:
    """The 502 body advertises the ``sac host add-peer`` remediation
    so the loud failure points at the fix."""
    # Arrange
    r = missing_peer_token_response
    # Act
    err = r.json().get("error", "")
    # Assert
    assert "sac host add-peer" in err


# ---------------------------------------------------------------------------
# ADR-0015 Stage 2 — ssh-transport selector + e2e ACL.
#
# When ``target_host`` is a member of ``host_config.peers`` (typical for
# real WAN hostnames the operator already has ssh trust to, e.g.
# ``ywata-note-win``), the forwarder swaps the HTTP leg for ssh + remote
# curl. The destination's ACL is unchanged — the receiver still gates
# on ``metadata.from_agent`` against its own ``comms_grants`` table.
#
# These tests exercise the full ssh leg without mocking subprocess: an
# ``ssh`` shim binary on ``$PATH`` performs the *real* httpx POST to the
# loopback uvicorn instance that stands in for the destination host.
# ---------------------------------------------------------------------------


def _write_peers_config(*, tmp_path: Path, target_host: str, ssh_target: str) -> Path:
    """Write a minimal ``config.yaml`` with one peer mapping.

    The forwarder calls ``host_config.load()`` which honours
    ``SCITEX_AGENT_CONTAINER_CONFIG`` — the caller is expected to set
    that env var to the returned path before the request fires.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg = {
        "host": {"canonical": "host-b"},
        "peers": {target_host: {"ssh": ssh_target}},
    }
    cfg_path.write_text(yaml.safe_dump(cfg))
    return cfg_path


@pytest.fixture
def cross_host_ssh_env(cross_host_env, ssh_http_shim, env_save_restore):
    """Extend ``cross_host_env`` with the ssh shim + a ``peers:`` config.

    Produces the topology the ssh-transport tests share:

    * A real uvicorn listen acts as host A (the destination, owns
      ``alice``). ``$peer-tokens/host-a.token`` is already seeded by
      the parent fixture.
    * A peers config that names ``host-a`` (the canonical host name on
      ``alice``'s ``instances`` row) with ``ssh: host-a-via-shim`` —
      that's the literal token the shim will see as its ssh host
      argument. Membership flips the forwarder onto the ssh leg.
    * The ssh shim is installed at the front of ``$PATH`` so the
      production code's ``subprocess.run(["ssh", ...])`` call lands
      on our shim instead of a real ``ssh``. The shim performs a
      real loopback ``httpx`` POST into host A's uvicorn — no mocks.

    Returned dict carries: ``db``, ``tmp``, ``host_a_port`` (free
    port reserved for host A's uvicorn), ``host_b_port`` (free port
    for the forwarder's TestClient session), ``shim`` (the
    :class:`_SshHttpShim` controller), ``token`` (SHARED_TOKEN).
    """
    # Arrange
    tmp = cross_host_env["tmp"]
    cfg_path = _write_peers_config(
        tmp_path=tmp, target_host="host-a", ssh_target="host-a-via-shim"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))
    ssh_http_shim.install()
    host_a_port = _free_port()
    host_b_port = _free_port()
    yield {
        "db": cross_host_env["db"],
        "tmp": tmp,
        "host_a_port": host_a_port,
        "host_b_port": host_b_port,
        "shim": ssh_http_shim,
        "token": SHARED_TOKEN,
    }


def _drive_ssh_cross_host_send(
    cross_host_ssh_env: dict,
    *,
    sender: str,
    text: str,
) -> dict:
    """Subscribe on host A as ``alice``, POST to host B for ``alice``
    with ``from_agent=sender``, return the SSE event host A receives.

    Both hosts run as real ``uvicorn`` instances; host B's forwarder
    uses the ssh shim (which performs a real httpx POST into host A's
    uvicorn). Mirrors :func:`test_cross_host_send_forwards_to_target_host`
    one section above — the only difference is the transport.
    """
    db = cross_host_ssh_env["db"]
    host_a_port = cross_host_ssh_env["host_a_port"]
    host_b_port = cross_host_ssh_env["host_b_port"]

    state_db.record_instance_start(name="alice", host="host-a", a2a_port=0, db_path=db)
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )

    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> dict:
        with _run_loopback(app_a, host_a_port):
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{host_a_port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=LOOPBACK_IO_TIMEOUT_S)
                with _run_loopback(app_b, host_b_port):
                    async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                        resp = await ac.post(
                            f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                            json=_send_payload(text, from_agent=sender),
                            headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                        )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"ssh forward returned {resp.status_code}: {resp.text!r}"
                    )
                await asyncio.wait_for(sub, timeout=LOOPBACK_IO_TIMEOUT_S)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    return asyncio.run(driver())


def test_cross_host_send_via_ssh_shim_delivers_to_remote_inbox(
    cross_host_ssh_env, pg_schema: str,
) -> None:
    """End-to-end ssh-transport: a POST to host B's ``message:send`` for
    a target pinned to host A arrives on host A's broker through the
    ssh-shim leg.
    """
    # Arrange
    db = cross_host_ssh_env["db"]
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    # Act
    event = _drive_ssh_cross_host_send(
        cross_host_ssh_env, sender="permitted-peer", text="hi via ssh"
    )
    # Assert
    assert event.get("content") == "hi via ssh"


def test_cross_host_send_via_ssh_shim_preserves_from_agent_metadata(
    cross_host_ssh_env, pg_schema: str,
) -> None:
    """The forwarded event keeps the original ``from_agent`` across the
    ssh transport so host A's ACL gates on the real sender, not the
    forwarding host's identity.
    """
    # Arrange
    db = cross_host_ssh_env["db"]
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    # Act
    event = _drive_ssh_cross_host_send(
        cross_host_ssh_env, sender="permitted-peer", text="probe"
    )
    # Assert
    assert event.get("from_agent") == "permitted-peer"


def test_cross_host_send_with_explicit_grant_unblocks_cross_group_push(
    cross_host_ssh_env, pg_schema: str,
) -> None:
    """A cross-group send delivered across the ssh transport lands at the
    destination. (Under messaging DEFAULT-ALLOW, operator 2026-07-03, the
    grant is redundant — cross-group already allows — but the grant path
    still works and the message must arrive at the receiver's inbox.)
    """
    # Arrange — sender lives under a SEPARATE root from alice. The grant is
    # written on the destination side and the message must arrive at the
    # receiver's inbox across the transport. The two apps agree about the
    # grant because they share one in-process SCITEX_STORE_DSN, NOT because
    # the fixture's tmp HOME pins them to a file: comms_grants is PostgreSQL
    # now. record_lineage below still takes db_path — lineage is still SQLite.
    db = cross_host_ssh_env["db"]
    record_lineage(child="alice", parent="root-a", db_path=db)
    record_lineage(child="outsider", parent="root-b", db_path=db)
    # db_path is gone from the grants primitives — that store is on
    # PostgreSQL and isolates via SCITEX_STORE_DSN (the pg_schema fixture).
    # record_lineage above KEEPS its db_path: that module is still on SQLite.
    state_db_nodes_grant.grant_send(
        sender="outsider",
        target="alice",
        note="ADR-0015 stage2 e2e test grant",
    )
    # Act
    event = _drive_ssh_cross_host_send(
        cross_host_ssh_env, sender="outsider", text="cross-group"
    )
    # Assert
    assert event.get("content") == "cross-group"


def test_cross_host_send_without_grant_returns_403_from_target_listen(
    cross_host_ssh_env, pg_schema: str,
) -> None:
    """A receiver-side ACL deny must surface across the ssh transport as
    a non-2xx response to the originating sender (loud failure, no silent
    drop). Since messaging is now DEFAULT-ALLOW cross-group (operator
    2026-07-03), the deny is triggered by a per-spec ``inbound.siblings=
    deny`` on the receiver (``alice``); ``outsider`` is a sibling.
    """
    # Arrange
    db = cross_host_ssh_env["db"]
    host_a_port = cross_host_ssh_env["host_a_port"]
    host_b_port = cross_host_ssh_env["host_b_port"]
    record_lineage(child="alice", parent="root", db_path=db)
    record_lineage(child="outsider", parent="root", db_path=db)
    state_db_nodes_grant.record_comms_policy(
        name="alice", inbound_siblings="deny"
    )
    state_db.record_instance_start(name="alice", host="host-a", a2a_port=0, db_path=db)
    with state_db.open_db(db) as conn:
        conn.execute(
            "UPDATE instances SET a2a_port = ? WHERE name = 'alice'",
            (host_a_port,),
        )
    app_a = create_app(token=SHARED_TOKEN, local_host="host-a")
    app_b = create_app(token=SHARED_TOKEN, local_host="host-b")

    async def driver() -> int:
        with _run_loopback(app_a, host_a_port), _run_loopback(app_b, host_b_port):
            async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                resp = await ac.post(
                    f"http://127.0.0.1:{host_b_port}/agents/alice/message:send",
                    json=_send_payload("denied", from_agent="outsider"),
                    headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                )
            return resp.status_code

    # Act
    status = asyncio.run(driver())
    # Assert
    assert status == 403


def test_cross_host_send_via_ssh_shim_uses_peer_token_bearer_header(
    cross_host_ssh_env, pg_schema: str,
) -> None:
    """The shim's captured Authorization header matches the destination
    host's bearer (``peer-tokens/host-a.token``) — proves the forwarder
    rotated to the *destination's* token, not its own listen token.
    """
    # Arrange
    db = cross_host_ssh_env["db"]
    record_lineage(child="permitted-peer", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    _drive_ssh_cross_host_send(
        cross_host_ssh_env, sender="permitted-peer", text="bearer probe"
    )
    # Act
    record = cross_host_ssh_env["shim"].last()
    # Assert
    assert record is not None and record.get("bearer") == SHARED_TOKEN


# ---------------------------------------------------------------------------
# Auto-ack loop-guard propagation (#140 follow-up): the ``ack`` marker a
# sender stamps under ``params.metadata.ack`` must survive minting so the
# receiving adapter's ``_should_auto_ack`` sees it and declines to ack an
# ack — otherwise two auto-ack adapters ping-pong forever. Real uvicorn +
# SSE round-trip on one host (handoff §0 — no mocks).
# ---------------------------------------------------------------------------


def _send_payload_with_meta(text: str, *, metadata: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "metadata": metadata,
        },
    }


def _roundtrip_local_send(cross_host_env, *, metadata: dict) -> dict:
    """POST a ``message:send`` to a single local host and return the
    event the SSE inbox subscriber receives. ``alice`` has no instance
    row so the target resolves local (no cross-host forward); sender
    ``bob`` and target ``alice`` are siblings under ``root`` so the
    intra-group ACL allows the send.
    """
    db = cross_host_env["db"]
    record_lineage(child="bob", parent="root", db_path=db)
    record_lineage(child="alice", parent="root", db_path=db)
    port = _free_port()
    app = create_app(token=SHARED_TOKEN, local_host="host-a")

    async def driver() -> dict:
        with _run_loopback(app, port):
            ready = asyncio.Event()
            captured: dict = {}

            async def consume():
                async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                    async with ac.stream(
                        "GET",
                        f"http://127.0.0.1:{port}/agents/alice/inbox/stream",
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    ) as sse:
                        async for line in sse.aiter_lines():
                            if line.startswith(":"):
                                ready.set()
                                continue
                            if line.startswith("data:"):
                                captured["event"] = json.loads(
                                    line[len("data:") :].lstrip()
                                )
                                return

            sub = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), timeout=LOOPBACK_IO_TIMEOUT_S)
                async with httpx.AsyncClient(timeout=LOOPBACK_IO_TIMEOUT_S) as ac:
                    await ac.post(
                        f"http://127.0.0.1:{port}/agents/alice/message:send",
                        json=_send_payload_with_meta("ping", metadata=metadata),
                        headers={"authorization": f"Bearer {SHARED_TOKEN}"},
                    )
                await asyncio.wait_for(sub, timeout=LOOPBACK_IO_TIMEOUT_S)
            finally:
                if not sub.done():
                    sub.cancel()
                    with contextlib.suppress(BaseException):
                        await sub
            return captured.get("event", {})

    return asyncio.run(driver())


def test_message_send_with_ack_metadata_yields_ack_true_event(
    cross_host_env, pg_schema: str,
) -> None:
    # Arrange
    metadata = {"from_agent": "bob", "ack": True}
    # Act
    event = _roundtrip_local_send(cross_host_env, metadata=metadata)
    # Assert
    assert event.get("ack") is True


def test_message_send_without_ack_metadata_yields_falsey_ack_event(
    cross_host_env, pg_schema: str,
) -> None:
    # Arrange
    metadata = {"from_agent": "bob"}
    # Act
    event = _roundtrip_local_send(cross_host_env, metadata=metadata)
    # Assert
    assert not event.get("ack")


def test_message_send_threads_dispatch_id_into_published_event(
    cross_host_env, pg_schema: str,
) -> None:
    # Arrange
    # The sender-minted dispatch_id must ride from metadata onto the
    # published inbox event so the channel wake path can thread it onto
    # the woken turn for requester-completion correlation.
    metadata = {"from_agent": "bob", "dispatch_id": "d-route-99"}
    # Act
    event = _roundtrip_local_send(cross_host_env, metadata=metadata)
    # Assert
    assert event.get("dispatch_id") == "d-route-99"


# ---------------------------------------------------------------------------
# Re-export surface — a missing one degraded SILENTLY
#
# `_resolve_runtime_self_identity` moved into `_agents_list` alongside
# `list_agents`, but only `list_agents` was re-exported here. Both self-peer
# callers import it inside a try/except that falls back to a warning, so the
# ImportError disabled self-peer persistence WITHOUT failing anything: listen
# kept serving and logged "continues without persisted self-peers". It reached
# develop and survived there, because nothing asserted the import.
#
# These assert the import SITE, not the behaviour, which is the thing that
# actually broke. Delete either name from the re-export and they go red.
# ---------------------------------------------------------------------------


def test_server_reexports_resolve_runtime_self_identity() -> None:
    # Arrange — the historical import path three callers still use.
    from scitex_agent_container._listen import server

    # Act
    resolved = getattr(server, "_resolve_runtime_self_identity", None)

    # Assert
    assert callable(resolved)


def test_server_reexports_list_agents() -> None:
    # Arrange — the sibling that WAS re-exported; guards the pair.
    from scitex_agent_container._listen import server

    # Act
    resolved = getattr(server, "list_agents", None)

    # Assert
    assert callable(resolved)


def test_self_peer_persistence_can_resolve_identity_without_falling_back() -> None:
    # Arrange — the real consumer whose except-branch masked the breakage.
    from scitex_agent_container._listen import _self_peer_persistence  # noqa: F401

    # Act
    from scitex_agent_container._listen.server import (
        _resolve_runtime_self_identity,
    )

    # Assert — resolving to None is a legitimate answer (no `lead:` block);
    # raising ImportError is not, and that is what this pins.
    assert _resolve_runtime_self_identity() is None or isinstance(
        _resolve_runtime_self_identity(), str
    )


# ---------------------------------------------------------------------------
# The FAMILY guard, not just this instance
#
# Fixing one missing re-export leaves the class unguarded: any symbol imported
# from `_listen.server` that this module does not export fails the same way,
# and both self-peer call sites absorb that ImportError into a warning, so it
# degrades silently instead of failing. `_resolve_runtime_self_identity` sat
# broken on develop for exactly that reason.
#
# This scans the shipped package for every `from ... _listen.server import X`
# and asserts X actually resolves. It catches the whole family at import-graph
# level, without needing anyone to have anticipated the specific symbol.
# ---------------------------------------------------------------------------


def _symbols_imported_from_listen_server() -> list[tuple[str, str]]:
    """Return (filename, symbol) for every import taken from `_listen.server`.

    Covers all three spellings in the tree: `from .server import X` inside
    `_listen/`, `from .._listen.server import X`, and the absolute form.
    """
    import ast
    from pathlib import Path

    from scitex_agent_container._listen import server as _server

    listen_dir = Path(_server.__file__).parent
    pkg_root = listen_dir.parent
    found: list[tuple[str, str]] = []
    for py in pkg_root.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            targets = (
                (node.level == 1 and mod == "server" and py.parent == listen_dir)
                or (node.level >= 1 and mod.endswith("_listen.server"))
                or (node.level == 0 and mod.endswith("_listen.server"))
            )
            if targets:
                found.extend((py.name, a.name) for a in node.names if a.name != "*")
    return found


def test_listen_server_import_scan_is_not_vacuous() -> None:
    # Arrange — a scan that finds nothing would make the guard below pass
    # for the wrong reason. This is that guard's positive control.
    expected_at_least = 2

    # Act
    found = _symbols_imported_from_listen_server()

    # Assert
    assert len(found) >= expected_at_least, f"scan found only {found}"


def test_every_symbol_imported_from_listen_server_resolves() -> None:
    # Arrange
    from scitex_agent_container._listen import server as _server

    requested = _symbols_imported_from_listen_server()

    # Act
    missing = [(f, n) for f, n in requested if not hasattr(_server, n)]

    # Assert
    assert not missing, (
        "imported from _listen.server but not exported by it — this fails "
        f"SILENTLY at runtime where the caller catches ImportError: {missing}"
    )
