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
