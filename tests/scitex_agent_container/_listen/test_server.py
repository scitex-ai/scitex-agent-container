"""Tests for the sac listen Starlette app.

Uses Starlette's TestClient (synchronous wrapper over the ASGI app) so
we exercise the real middleware + routes without spinning uvicorn.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-1234567890"


def _seed_agent(tmp_path: Path, name: str, session_id: str | None) -> None:
    yaml_root = tmp_path / "agents"
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(
        f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  workdir: {tmp_path / "workdir"}
"""
    )
    (tmp_path / "workdir").mkdir(exist_ok=True)
    state_dir = tmp_path / "state" / name
    state_dir.mkdir(parents=True)
    if session_id:
        (state_dir / "session_id").write_text(session_id, encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    _seed_agent(tmp_path, "alpha", "sess-abc")
    _seed_agent(tmp_path, "ghost", None)  # no session_id
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    app = create_app(token=TOKEN)
    return TestClient(app), tmp_path


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# --- Auth ---------------------------------------------------------------


def test_health_is_public(client):
    c, _ = client
    r = c.get("/v1/sac/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_missing_token_is_401(client):
    c, _ = client
    r = c.get("/v1/sac/agents")
    assert r.status_code == 401
    assert "missing" in r.json()["error"]


def test_wrong_token_is_403(client):
    c, _ = client
    r = c.get("/v1/sac/agents", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_bearer_scheme_case_insensitive(client):
    c, _ = client
    r = c.get("/v1/sac/agents", headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 200


# --- Status + list ------------------------------------------------------


def test_list_agents_returns_array(client):
    c, _ = client
    r = c.get("/v1/sac/agents", headers=auth_headers())
    assert r.status_code == 200
    assert "agents" in r.json()
    assert isinstance(r.json()["agents"], list)


def test_status_returns_session_id_and_workdir(client):
    c, tmp_path = client
    r = c.get("/v1/sac/agents/alpha/status", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "alpha"
    assert body["session_id"] == "sess-abc"
    assert body["workdir"] == str(tmp_path / "workdir")


def test_status_unknown_agent_is_404(client):
    c, _ = client
    r = c.get("/v1/sac/agents/does-not-exist/status", headers=auth_headers())
    assert r.status_code == 404


# --- send ---------------------------------------------------------------


def test_send_requires_json(client):
    c, _ = client
    r = c.post(
        "/v1/sac/agents/alpha/send",
        data="not-json",
        headers=auth_headers(),
    )
    assert r.status_code == 400


def test_send_key_returns_501(client):
    c, _ = client
    r = c.post(
        "/v1/sac/agents/alpha/send",
        json={"type": "key", "key": "ESC"},
        headers=auth_headers(),
    )
    assert r.status_code == 501


def test_send_missing_prompt_is_400(client):
    c, _ = client
    r = c.post(
        "/v1/sac/agents/alpha/send",
        json={"type": "prompt"},
        headers=auth_headers(),
    )
    assert r.status_code == 400


def test_send_no_session_id_is_409(client):
    c, _ = client
    r = c.post(
        "/v1/sac/agents/ghost/send",
        json={"prompt": "hello"},
        headers=auth_headers(),
    )
    assert r.status_code == 409


def test_send_unknown_agent_is_404(client):
    c, _ = client
    r = c.post(
        "/v1/sac/agents/missing/send",
        json={"prompt": "hello"},
        headers=auth_headers(),
    )
    assert r.status_code == 404


def test_send_happy_path_invokes_claude(client):
    c, tmp_path = client

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    captured: dict = {}

    def fake_run(argv, cwd=None, **_kwargs):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return FakeProc()

    with (
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/usr/local/bin/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        r = c.post(
            "/v1/sac/agents/alpha/send",
            json={"prompt": "follow up", "options": {"model": "opus", "max_turns": 2}},
            headers=auth_headers(),
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "alpha"
    assert body["session_id"] == "sess-abc"
    assert body["returncode"] == 0
    assert body["stdout"] == "ok"
    # And the claude argv was built correctly with --resume + model + max-turns
    argv = captured["argv"]
    assert argv[:5] == [
        "/usr/local/bin/claude",
        "--resume",
        "sess-abc",
        "-p",
        "follow up",
    ]
    assert "--model" in argv and "opus" in argv
    assert "--max-turns" in argv and "2" in argv
    assert captured["cwd"] == str(tmp_path / "workdir")


# --- delete -------------------------------------------------------------


def _seed_agent_with_a2a(tmp_path: Path, name: str, port: int) -> None:
    """Same as _seed_agent but with spec.a2a.port set."""
    yaml_root = tmp_path / "agents"
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "spec.yaml").write_text(
        f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  workdir: {tmp_path / "workdir"}
  a2a:
    host: 127.0.0.1
    port: {port}
"""
    )
    state_dir = tmp_path / "state" / name
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "session_id").write_text("sess-zzz", encoding="utf-8")


def test_send_routes_to_live_runner_when_a2a_port_set(tmp_path, monkeypatch):
    """If the agent has a reachable inbound HTTP, /send forwards there
    instead of re-launching claude."""
    (tmp_path / "workdir").mkdir(exist_ok=True)
    _seed_agent_with_a2a(tmp_path, "live", port=9999)
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    app = create_app(token=TOKEN)
    c = TestClient(app)

    # Mock the live runner: pretend POST /v1/turn returned 200 with a reply.
    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            import json as _json

            return _json.dumps({"reply": "live-runner answered"}).encode()

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp()

    with patch(
        "scitex_agent_container._listen.server._urlrequest.urlopen",
        side_effect=fake_urlopen,
    ):
        r = c.post(
            "/v1/sac/agents/live/send",
            json={"prompt": "hello live"},
            headers=auth_headers(),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["route"] == "live-runner"
    assert body["reply"] == "live-runner answered"
    assert captured["url"] == "http://127.0.0.1:9999/v1/turn"


def test_send_falls_back_when_live_runner_unreachable(tmp_path, monkeypatch):
    """If spec.a2a.port is set but the listener is down, fall back to
    the short-lived claude --resume path."""
    (tmp_path / "workdir").mkdir(exist_ok=True)
    _seed_agent_with_a2a(tmp_path, "down", port=9998)
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    app = create_app(token=TOKEN)
    c = TestClient(app)

    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    class FakeProc:
        returncode = 0
        stdout = "fallback ok"
        stderr = ""

    with (
        patch(
            "scitex_agent_container._listen.server._urlrequest.urlopen",
            side_effect=boom,
        ),
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/usr/local/bin/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            return_value=FakeProc(),
        ),
    ):
        r = c.post(
            "/v1/sac/agents/down/send",
            json={"prompt": "hello fallback"},
            headers=auth_headers(),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # No 'route' key when we fell through (short-lived path uses different schema)
    assert "returncode" in body
    assert body["returncode"] == 0


def test_post_agents_rejects_non_json(client):
    c, _ = client
    r = c.post("/v1/sac/agents", data="x", headers=auth_headers())
    assert r.status_code == 400


def test_post_agents_requires_name(client):
    c, _ = client
    r = c.post("/v1/sac/agents", json={}, headers=auth_headers())
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_post_agents_shells_out_to_sac_agent_start(client):
    c, _ = client

    class FakeProc:
        returncode = 0
        stdout = "started ok"
        stderr = ""

    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return FakeProc()

    with (
        patch(
            "scitex_agent_container._listen.server.shutil.which",
            return_value="/usr/bin/sac",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        r = c.post("/v1/sac/agents", json={"name": "alpha"}, headers=auth_headers())
    assert r.status_code == 200, r.text
    assert captured["argv"] == ["/usr/bin/sac", "agent", "start", "alpha"]
    body = r.json()
    assert body["name"] == "alpha"
    assert body["returncode"] == 0


def test_card_returns_a2a_shape(client):
    c, _ = client
    r = c.get("/v1/sac/agents/alpha/card", headers=auth_headers())
    assert r.status_code == 200, r.text
    card = r.json()
    # AgentCard required fields per A2A v0.3+: name, description, url, version, capabilities
    assert "name" in card
    assert "url" in card
    assert card["url"].startswith("http://")
    assert "/v1/sac/a2a" in card["url"]


def test_card_unknown_agent_is_404(client):
    c, _ = client
    r = c.get("/v1/sac/agents/does-not-exist/card", headers=auth_headers())
    assert r.status_code == 404


def test_delete_no_pid_file_is_404(client):
    c, _ = client
    r = c.delete("/v1/sac/agents/alpha", headers=auth_headers())
    assert r.status_code == 404


def test_delete_signals_pid(client):
    c, tmp_path = client
    # seed a pid file with a value we can intercept
    pid_file = tmp_path / "state" / "alpha" / "pid"
    pid_file.write_text("12345", encoding="utf-8")

    killed: dict = {}

    def fake_kill(pid, sig):
        killed["pid"] = pid
        killed["sig"] = sig

    with patch("scitex_agent_container._listen.server.os.kill", side_effect=fake_kill):
        r = c.delete("/v1/sac/agents/alpha", headers=auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] is True
    assert killed == {"pid": 12345, "sig": 15}
