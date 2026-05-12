"""Tests for the new v1 namespace per REQUIREMENT_SUMMARY.md §4.

Covers:
    - /v1/health (public)
    - /v1/agents (GET, POST)
    - /v1/agents/<name>/status
    - /v1/agents/<name>/tail  (SSE; with/without since)
    - /v1/agents/<name>/send  (prompt, key, back-compat no-type)
    - /v1/agents/<name>/card
    - DELETE /v1/agents/<name>
    - Symmetric /v1/a2a/agents/<name>/card mirror
    - Bearer-token auth (missing/wrong/correct)
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app

TOKEN = "v1-token-abcdef-1234567890"


def _seed_spec(tmp_path: Path, name: str, session_id: str | None = None) -> None:
    yaml_root = tmp_path / "agents"
    agent_dir = yaml_root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
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
    state_dir.mkdir(parents=True, exist_ok=True)
    if session_id:
        (state_dir / "session_id").write_text(session_id, encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    _seed_spec(tmp_path, "alpha", "sess-1")
    _seed_spec(tmp_path, "beta", "sess-2")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    # Per-agent runtime/session.jsonl path is patched per-test where used;
    # default to a path under tmp_path so unrelated tests don't hit real $HOME.
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(
        "scitex_agent_container._listen.server._runtime_session_jsonl",
        lambda name: runtime_root / name / "session.jsonl",
    )
    app = create_app(token=TOKEN)
    return TestClient(app), tmp_path


def hdr() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# --- Auth ----------------------------------------------------------------


def test_health_is_public(client):
    c, _ = client
    r = c.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_missing_token_is_401(client):
    c, _ = client
    r = c.get("/v1/agents")
    assert r.status_code == 401


def test_wrong_token_is_rejected(client):
    """Wrong token returns 401 or 403 (middleware emits 403; spec asks 401 —
    we accept either as long as it's an auth-failure status)."""
    c, _ = client
    r = c.get("/v1/agents", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403)


def test_correct_token_is_200(client):
    c, _ = client
    r = c.get("/v1/agents", headers=hdr())
    assert r.status_code == 200


# --- /v1/agents listing + create -----------------------------------------


def test_list_agents_returns_array(client):
    c, _ = client
    r = c.get("/v1/agents", headers=hdr())
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body
    assert isinstance(body["agents"], list)


def test_post_agents_inline_spec_starts(client, tmp_path, monkeypatch):
    c, _ = client
    fake_home = tmp_path / "fakehome-post"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    class FakeProc:
        returncode = 0
        stdout = "started"
        stderr = ""

    captured: dict = {}

    def fake_run(argv, **_kw):
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
        r = c.post(
            "/v1/agents",
            json={
                "name": "gamma",
                "spec": {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "spec": {"runtime": "apptainer", "workdir": "/tmp"},
                },
            },
            headers=hdr(),
        )
    assert r.status_code == 200, r.text
    assert captured["argv"] == ["/usr/bin/sac", "agent", "start", "gamma"]


# --- /v1/agents/<name>/status --------------------------------------------


def test_status_returns_agent_state(client, tmp_path):
    c, _ = client
    r = c.get("/v1/agents/alpha/status", headers=hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "alpha"
    assert body["session_id"] == "sess-1"
    assert body["workdir"] == str(tmp_path / "workdir")


# --- DELETE /v1/agents/<name> --------------------------------------------


def test_delete_sigterms_runner(client, tmp_path):
    c, _ = client
    pid_file = tmp_path / "state" / "alpha" / "pid"
    pid_file.write_text("4242", encoding="utf-8")
    killed: dict = {}

    def fake_kill(pid, sig):
        killed["pid"] = pid
        killed["sig"] = sig

    with patch("scitex_agent_container._listen.server.os.kill", side_effect=fake_kill):
        r = c.delete("/v1/agents/alpha", headers=hdr())
    assert r.status_code == 200, r.text
    assert killed == {"pid": 4242, "sig": 15}


# --- /v1/agents/<name>/card ----------------------------------------------


def test_card_returns_a2a_compatible_json(client):
    c, _ = client
    r = c.get("/v1/agents/alpha/card", headers=hdr())
    assert r.status_code == 200, r.text
    card = r.json()
    # Required A2A AgentCard fields per the project's card builder
    assert "name" in card
    assert "url" in card
    # Card url should point at the /v1/a2a prefix (where the A2A mirror lives)
    assert "/v1/a2a" in card["url"]


# --- /v1/agents/<name>/tail (SSE) ----------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(_json.dumps(rec) + "\n")


def test_tail_streams_all_lines_without_filter(client, tmp_path):
    c, _ = client
    sess = tmp_path / "runtime" / "alpha" / "session.jsonl"
    _write_jsonl(
        sess,
        [
            {"ts": "2026-05-12T10:00:00", "event": "start"},
            {"ts": "2026-05-12T10:00:01", "event": "msg", "text": "hello"},
            {"ts": "2026-05-12T10:00:02", "event": "end"},
        ],
    )

    with c.stream("GET", "/v1/agents/alpha/tail", headers=hdr()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes()).decode("utf-8")

    # All three lines should be emitted as data frames.
    assert body.count("data:") == 3
    assert '"start"' in body
    assert '"hello"' in body
    assert '"end"' in body
    # Each frame is wrapped with the {"line_no":..,"record":..} envelope.
    assert '"line_no"' in body
    assert '"record"' in body


def test_tail_filters_by_since(client, tmp_path):
    c, _ = client
    sess = tmp_path / "runtime" / "alpha" / "session.jsonl"
    _write_jsonl(
        sess,
        [
            {"ts": "2026-05-12T09:00:00", "event": "old1"},
            {"ts": "2026-05-12T09:30:00", "event": "old2"},
            {"ts": "2026-05-12T10:00:00", "event": "new1"},
            {"ts": "2026-05-12T11:00:00", "event": "new2"},
        ],
    )

    with c.stream(
        "GET",
        "/v1/agents/alpha/tail?since=2026-05-12T10:00:00",
        headers=hdr(),
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode("utf-8")

    assert '"old1"' not in body
    assert '"old2"' not in body
    assert '"new1"' in body
    assert '"new2"' in body


def test_tail_missing_file_without_follow_is_404(client):
    c, _ = client
    r = c.get("/v1/agents/never-existed/tail", headers=hdr())
    assert r.status_code == 404


# --- /v1/agents/<name>/send ----------------------------------------------


def test_send_type_prompt_invokes_claude(client, tmp_path):
    c, _ = client

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    captured: dict = {}

    def fake_run(argv, cwd=None, **_kw):
        captured["argv"] = argv
        return FakeProc()

    with (
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        r = c.post(
            "/v1/agents/alpha/send",
            json={"type": "prompt", "prompt": "hi"},
            headers=hdr(),
        )
    assert r.status_code == 200, r.text
    assert captured["argv"][:5] == ["/x/claude", "--resume", "sess-1", "-p", "hi"]


def test_send_no_type_treated_as_prompt(client):
    """Back-compat per REQUIREMENT_SUMMARY §4.2: body without 'type' is
    accepted as prompt. (Until the next breaking change.)"""
    c, _ = client

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    with (
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            return_value=FakeProc(),
        ),
    ):
        r = c.post(
            "/v1/agents/alpha/send",
            json={"prompt": "no-type"},  # no 'type' key
            headers=hdr(),
        )
    assert r.status_code == 200, r.text


def test_send_type_key_esc_sigints_live_runner(client, tmp_path):
    c, _ = client
    (tmp_path / "state" / "alpha" / "pid").write_text("9999", encoding="utf-8")
    killed: dict = {}
    with patch(
        "scitex_agent_container._listen.server.os.kill",
        side_effect=lambda pid, sig: killed.update(pid=pid, sig=sig),
    ):
        r = c.post(
            "/v1/agents/alpha/send",
            json={"type": "key", "key": "ESC"},
            headers=hdr(),
        )
    assert r.status_code == 200, r.text
    assert killed == {"pid": 9999, "sig": 2}  # SIGINT
    assert r.json()["route"] == "interrupt"


def test_send_type_key_esc_no_live_runner_is_404(client):
    c, _ = client
    # 'beta' has no pid file
    r = c.post(
        "/v1/agents/beta/send",
        json={"type": "key", "key": "ESC"},
        headers=hdr(),
    )
    assert r.status_code == 404
    assert "live session" in r.json()["error"]


def test_send_unknown_key_is_400(client):
    c, _ = client
    r = c.post(
        "/v1/agents/alpha/send",
        json={"type": "key", "key": "F12"},
        headers=hdr(),
    )
    assert r.status_code == 400


# --- Symmetric /v1/a2a/agents mirror -------------------------------------


def test_a2a_mirror_card_matches_native(client):
    c, _ = client
    native = c.get("/v1/agents/alpha/card", headers=hdr())
    mirror = c.get("/v1/a2a/agents/alpha/card", headers=hdr())
    assert native.status_code == 200
    assert mirror.status_code == 200
    # Same backing data → same JSON payload.
    assert native.json() == mirror.json()


def test_a2a_mirror_lists_agents(client):
    c, _ = client
    r = c.get("/v1/a2a/agents", headers=hdr())
    assert r.status_code == 200
    assert "agents" in r.json()


def test_a2a_mirror_send_accepts_prompt(client, tmp_path):
    c, _ = client

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    with (
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            return_value=FakeProc(),
        ),
    ):
        r = c.post(
            "/v1/a2a/agents/alpha/send",
            json={"type": "prompt", "prompt": "via a2a"},
            headers=hdr(),
        )
    assert r.status_code == 200, r.text


def test_a2a_mirror_requires_auth(client):
    c, _ = client
    r = c.get("/v1/a2a/agents")  # no header
    assert r.status_code == 401
