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
    r = c.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_missing_token_is_401(client):
    c, _ = client
    r = c.get("/agents")
    assert r.status_code == 401
    assert "missing" in r.json()["error"]


def test_wrong_token_is_403(client):
    c, _ = client
    r = c.get("/agents", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_bearer_scheme_case_insensitive(client):
    c, _ = client
    r = c.get("/agents", headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == 200


# --- Status + list ------------------------------------------------------


def test_list_agents_returns_array(client):
    c, _ = client
    r = c.get("/agents", headers=auth_headers())
    assert r.status_code == 200
    assert "agents" in r.json()
    assert isinstance(r.json()["agents"], list)


def test_status_returns_session_id_and_workdir(client):
    c, tmp_path = client
    r = c.get("/agents/alpha/status", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "alpha"
    assert body["session_id"] == "sess-abc"
    assert body["workdir"] == str(tmp_path / "workdir")


def test_status_unknown_agent_is_404(client):
    c, _ = client
    r = c.get("/agents/does-not-exist/status", headers=auth_headers())
    assert r.status_code == 404


# --- send ---------------------------------------------------------------


def test_send_requires_json(client):
    c, _ = client
    r = c.post(
        "/agents/alpha/send",
        data="not-json",
        headers=auth_headers(),
    )
    assert r.status_code == 400


def test_send_key_esc_sends_sigint(client):
    c, tmp_path = client
    pid_file = tmp_path / "state" / "alpha" / "pid"
    pid_file.write_text("9876", encoding="utf-8")
    killed = {}
    with patch(
        "scitex_agent_container._listen.server.os.kill",
        side_effect=lambda pid, sig: killed.update(pid=pid, sig=sig),
    ):
        r = c.post(
            "/agents/alpha/send",
            json={"type": "key", "key": "ESC"},
            headers=auth_headers(),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["route"] == "interrupt"
    assert killed == {"pid": 9876, "sig": 2}


def test_send_key_unknown_is_400(client):
    c, _ = client
    r = c.post(
        "/agents/alpha/send",
        json={"type": "key", "key": "F12"},
        headers=auth_headers(),
    )
    assert r.status_code == 400


def test_send_missing_prompt_is_400(client):
    c, _ = client
    r = c.post(
        "/agents/alpha/send",
        json={"type": "prompt"},
        headers=auth_headers(),
    )
    assert r.status_code == 400


def test_send_no_session_id_is_409(client):
    c, _ = client
    r = c.post(
        "/agents/ghost/send",
        json={"prompt": "hello"},
        headers=auth_headers(),
    )
    assert r.status_code == 409


def test_send_unknown_agent_is_404(client):
    c, _ = client
    r = c.post(
        "/agents/missing/send",
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
            "/agents/alpha/send",
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
            "/agents/live/send",
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
            "/agents/down/send",
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
    r = c.post("/agents", data="x", headers=auth_headers())
    assert r.status_code == 400


def test_post_agents_requires_name(client):
    c, _ = client
    r = c.post("/agents", json={}, headers=auth_headers())
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_post_agents_inline_spec_writes_file_and_starts(client, tmp_path, monkeypatch):
    """Inline ``spec`` body should be written to the install root and then
    handed off to ``sac agent start``."""
    c, _ = client
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    class FakeProc:
        returncode = 0
        stdout = "started"
        stderr = ""

    with (
        patch(
            "scitex_agent_container._listen.server.shutil.which",
            return_value="/usr/bin/sac",
        ),
        patch(
            "scitex_agent_container._listen.server.subprocess.run",
            return_value=FakeProc(),
        ),
    ):
        r = c.post(
            "/agents",
            json={
                "name": "adhoc-1",
                "spec": {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "spec": {"runtime": "apptainer", "workdir": "/tmp"},
                },
            },
            headers=auth_headers(),
        )
    assert r.status_code == 200, r.text
    written = (
        fake_home / ".scitex" / "agent-container" / "agents" / "adhoc-1" / "spec.yaml"
    )
    assert written.is_file()
    assert "scitex-agent-container/v3" in written.read_text()


def test_post_agents_inline_spec_rejects_wrong_apiversion(
    client, tmp_path, monkeypatch
):
    c, _ = client
    monkeypatch.setenv("HOME", str(tmp_path))
    r = c.post(
        "/agents",
        json={
            "name": "bad",
            "spec": {"apiVersion": "v1", "kind": "Agent"},
        },
        headers=auth_headers(),
    )
    assert r.status_code == 400
    assert "v3" in r.json()["error"]


def test_post_agents_inline_spec_conflicts_without_overwrite(
    client, tmp_path, monkeypatch
):
    """Re-POSTing the same name without ``overwrite: true`` is a 409."""
    c, _ = client
    fake_home = tmp_path / "fakehome2"
    target = fake_home / ".scitex" / "agent-container" / "agents" / "dup"
    target.mkdir(parents=True)
    (target / "spec.yaml").write_text("preexisting", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    r = c.post(
        "/agents",
        json={
            "name": "dup",
            "spec": {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {"runtime": "apptainer"},
            },
        },
        headers=auth_headers(),
    )
    assert r.status_code == 409
    # And the original file is untouched
    assert (target / "spec.yaml").read_text() == "preexisting"


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
        r = c.post("/agents", json={"name": "alpha"}, headers=auth_headers())
    assert r.status_code == 200, r.text
    assert captured["argv"] == ["/usr/bin/sac", "agent", "start", "alpha"]
    body = r.json()
    assert body["name"] == "alpha"
    assert body["returncode"] == 0


def test_card_returns_a2a_shape(client):
    c, _ = client
    r = c.get("/agents/alpha/card", headers=auth_headers())
    assert r.status_code == 200, r.text
    card = r.json()
    # ADR-0004 — A2A v1 AgentCard: `url` lives under supportedInterfaces[],
    # not at the top level.
    assert "name" in card
    assert card["supportedInterfaces"][0]["url"].startswith("http://")
    assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"


def test_card_unknown_agent_is_404(client):
    c, _ = client
    r = c.get("/agents/does-not-exist/card", headers=auth_headers())
    assert r.status_code == 404


def test_delete_no_pid_file_is_404(client):
    c, _ = client
    r = c.delete("/agents/alpha", headers=auth_headers())
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
        r = c.delete("/agents/alpha", headers=auth_headers())
    assert r.status_code == 200, r.text
    assert r.json()["stopped"] is True
    assert killed == {"pid": 12345, "sig": 15}


# --- SSE streaming ------------------------------------------------------


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, lines: list[bytes], returncode: int = 0) -> None:
        self.stdout = _FakeStdout(lines)
        self.stderr = None
        self._rc = returncode
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_send_sse_streams_stream_json_lines(client):
    """With Accept: text/event-stream, /send returns SSE frames built
    from claude's stream-json stdout, plus start/done events."""
    c, _ = client

    fake = _FakeProc(
        lines=[
            b'{"type":"assistant","text":"hi"}\n',
            b'{"type":"result","ok":true}\n',
        ],
        returncode=0,
    )

    async def fake_exec(*argv, **_kwargs):
        # also assert stream-json got appended to argv
        assert "--output-format" in argv
        assert "stream-json" in argv
        return fake

    with (
        patch(
            "scitex_agent_container._listen.server._find_claude_binary",
            return_value="/x/claude",
        ),
        patch(
            "scitex_agent_container._listen.server.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
    ):
        with c.stream(
            "POST",
            "/agents/alpha/send",
            json={"prompt": "go"},
            headers={**auth_headers(), "Accept": "text/event-stream"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = b"".join(r.iter_bytes()).decode("utf-8")

    # start frame mentions the session id
    assert "event: start" in body
    assert "sess-abc" in body
    # both stream-json lines come through as data frames
    assert '"assistant"' in body
    assert '"result"' in body
    # final done frame carries the returncode
    assert "event: done" in body
    assert '"returncode": 0' in body


# ---------------------------------------------------------------------------
# Merged from test_server_extra.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

import asyncio
import urllib.error

import pytest

from scitex_agent_container._listen._tail import (
    _parse_iso_ts,
    _record_ts,
    _runtime_session_jsonl,
    _sse_frame,
)
from scitex_agent_container._listen.server import (
    _find_claude_binary,
    _stream_claude,
)

TOKEN = "test-token-1234567890"


@pytest.fixture(autouse=True)
def _isolate_home_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _auth_extra() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed_spec_extra(
    tmp_path: Path,
    name: str,
    *,
    with_pid: int | None = None,
    with_session: str | None = "sess-abc",
) -> None:
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
    if with_session:
        (state_dir / "session_id").write_text(with_session, encoding="utf-8")
    if with_pid is not None:
        (state_dir / "pid").write_text(str(with_pid), encoding="utf-8")


@pytest.fixture
def client_extra(tmp_path, monkeypatch):
    _seed_spec_extra(tmp_path, "alpha")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(tmp_path / "agents"))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )
    app = create_app(token=TOKEN)
    return TestClient(app), tmp_path


# --- _find_claude_binary --------------------------------------------------


def test_find_claude_binary_uses_bundled(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("os.access", lambda p, mode: True)
    assert _find_claude_binary().endswith("/_bundled/claude")


def test_find_claude_binary_uses_path(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.shutil.which",
        lambda x: "/usr/bin/claude",
    )
    assert _find_claude_binary() == "/usr/bin/claude"


def test_find_claude_binary_missing_raises(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.shutil.which", lambda x: None
    )
    with pytest.raises(RuntimeError, match="claude binary not found"):
        _find_claude_binary()


# --- list_agents error path -----------------------------------------------


def test_list_agents_returns_500_on_registry_error(client_extra):
    c, _ = client_extra

    class Boom:
        def list_all(self):
            raise RuntimeError("registry broken")

    with patch(
        "scitex_agent_container._listen.server.Registry", side_effect=lambda: Boom()
    ):
        r = c.get("/agents", headers=_auth_extra())
    assert r.status_code == 500
    assert "registry broken" in r.json()["error"]


# --- _forward_to_live_runner HTTPError -----------------------------------


def test_send_live_runner_http_error_propagates_status(tmp_path, monkeypatch):
    (tmp_path / "workdir").mkdir(exist_ok=True)
    yaml_root = tmp_path / "agents"
    (yaml_root / "live").mkdir(parents=True)
    (yaml_root / "live" / "spec.yaml").write_text(
        f"""apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  workdir: {tmp_path / "workdir"}
  a2a:
    host: 127.0.0.1
    port: 9000
"""
    )
    state_dir = tmp_path / "state" / "live"
    state_dir.mkdir(parents=True)
    (state_dir / "session_id").write_text("sid", encoding="utf-8")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_root))
    monkeypatch.setattr(
        "scitex_agent_container._listen.server.state_dir_for",
        lambda name, root=None: tmp_path / "state" / name,
    )

    def boom(req, timeout=None):
        # HTTPError needs fp; mimic Python's: fp can be None but read() must work
        err = urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, None)
        err.read = lambda: b'{"detail":"upstream broke"}'  # type: ignore
        raise err

    app = create_app(token=TOKEN)
    c = TestClient(app)
    with patch(
        "scitex_agent_container._listen.server._urlrequest.urlopen",
        side_effect=boom,
    ):
        r = c.post("/agents/live/send", json={"prompt": "x"}, headers=_auth_extra())
    assert r.status_code == 502
    body = r.json()
    assert body["route"] == "live-runner"
    assert body["status"] == 502
    assert "upstream" in body["error"]


# --- agent_send unknown type ----------------------------------------------


def test_send_unknown_type_is_400(client_extra):
    c, _ = client_extra
    r = c.post(
        "/agents/alpha/send",
        json={"type": "weird"},
        headers=_auth_extra(),
    )
    assert r.status_code == 400
    assert "unknown type" in r.json()["error"]


def test_send_key_no_live_session_is_404(client_extra):
    c, _ = client_extra
    # no pid file seeded for alpha
    r = c.post(
        "/agents/alpha/send", json={"type": "key", "key": "C-c"}, headers=_auth_extra()
    )
    assert r.status_code == 404
    assert "no live session" in r.json()["error"]


def test_send_key_oskill_oserror_is_500(client_extra):
    c, tmp_path = client_extra
    (tmp_path / "state" / "alpha" / "pid").write_text("777", encoding="utf-8")

    def boom(pid, sig):
        raise OSError("no such process")

    with patch("scitex_agent_container._listen.server.os.kill", side_effect=boom):
        r = c.post(
            "/agents/alpha/send",
            json={"type": "key", "key": "ESC"},
            headers=_auth_extra(),
        )
    assert r.status_code == 500
    assert "no such process" in r.json()["error"]


def test_send_claude_binary_missing_is_500(client_extra):
    c, _ = client_extra
    with patch(
        "scitex_agent_container._listen.server._find_claude_binary",
        side_effect=RuntimeError("claude binary not found"),
    ):
        r = c.post("/agents/alpha/send", json={"prompt": "hi"}, headers=_auth_extra())
    assert r.status_code == 500
    assert "claude binary not found" in r.json()["error"]


# --- _stream_claude FileNotFoundError + cancellation ----------------------


def _drain_async(agen):
    out = []

    async def runner():
        async for chunk in agen:
            out.append(chunk)

    asyncio.run(runner())
    return out


def test_stream_claude_filenotfound_yields_error_frame(monkeypatch):
    async def boom(*a, **kw):
        raise FileNotFoundError("claude missing")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    frames = _drain_async(_stream_claude(["claude"], "/tmp", "name", "sid"))
    joined = b"".join(frames).decode("utf-8")
    assert "event: error" in joined
    assert "claude missing" in joined


def test_stream_claude_cancellation_terminates_proc(monkeypatch):
    class FakeStdout:
        def __init__(self):
            self.calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                return b'{"hello": "world"}\n'
            # next readline never returns — simulate hang
            await asyncio.sleep(60)
            return b""

    terminated = {"flag": False}

    class FakeProc:
        stdout = FakeStdout()
        stderr = None
        returncode: int | None = None

        async def wait(self):
            self.returncode = 0
            return 0

        def terminate(self):
            terminated["flag"] = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def make_proc(*a, **kw):
        return FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", make_proc)

    async def runner():
        gen = _stream_claude(["claude"], "/tmp", "n", "s")
        # consume start + first line, then close generator → CancelledError path
        await gen.__anext__()
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(runner())
    assert terminated["flag"] is True


# --- _parse_iso_ts / _record_ts -------------------------------------------


def test_parse_iso_ts_returns_none_for_empty():
    assert _parse_iso_ts("") is None
    assert _parse_iso_ts(123) is None  # type: ignore[arg-type]


def test_parse_iso_ts_handles_z_suffix():
    dt = _parse_iso_ts("2026-01-02T03:04:05Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 1 and dt.day == 2


def test_parse_iso_ts_returns_none_on_garbage():
    assert _parse_iso_ts("not-a-date") is None


def test_record_ts_prefers_ts_then_timestamp():
    assert _record_ts({"ts": "2026-05-01T00:00:00"}) is not None
    assert _record_ts({"timestamp": "2026-05-01T00:00:00"}) is not None
    assert _record_ts({}) is None
    assert _record_ts({"ts": None, "timestamp": "garbage"}) is None


# --- _runtime_session_jsonl default --------------------------------------


def test_runtime_session_jsonl_under_home(tmp_path):
    # Path.home is monkeypatched to tmp_path by autouse fixture
    p = _runtime_session_jsonl("alpha")
    assert "agent-container" in str(p)
    assert p.name == "session.jsonl"


# --- agent_tail -----------------------------------------------------------


def test_tail_404_when_missing_and_not_follow(client_extra, tmp_path, monkeypatch):
    c, _ = client_extra
    monkeypatch.setattr(
        "scitex_agent_container._listen._tail._runtime_session_jsonl",
        lambda name: tmp_path / "missing.jsonl",
    )
    r = c.get("/agents/alpha/tail", headers=_auth_extra())
    assert r.status_code == 404
    assert "no session.jsonl" in r.json()["error"]


def test_tail_streams_existing_lines(client_extra, tmp_path, monkeypatch):
    c, _ = client_extra
    jsonl = tmp_path / "alpha.jsonl"
    jsonl.write_text(
        '{"ts":"2026-05-01T00:00:00","msg":"a"}\n'
        "\n"  # empty line skipped
        "not-json-{{\n"  # malformed → {"raw": ...}
        '{"ts":"2026-05-01T01:00:00","msg":"b"}\n'
    )
    monkeypatch.setattr(
        "scitex_agent_container._listen._tail._runtime_session_jsonl",
        lambda name: jsonl,
    )
    with c.stream("GET", "/agents/alpha/tail", headers=_auth_extra()) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode("utf-8")
    assert '"msg": "a"' in body
    assert '"msg": "b"' in body
    assert '"raw"' in body  # malformed line surfaced


def test_tail_since_filter_drops_old_records(client_extra, tmp_path, monkeypatch):
    c, _ = client_extra
    jsonl = tmp_path / "alpha.jsonl"
    jsonl.write_text(
        '{"ts":"2026-01-01T00:00:00","msg":"old"}\n'
        '{"ts":"2026-06-01T00:00:00","msg":"new"}\n'
        '{"msg":"no-ts-after-cross"}\n'  # included once we've crossed
    )
    monkeypatch.setattr(
        "scitex_agent_container._listen._tail._runtime_session_jsonl",
        lambda name: jsonl,
    )
    with c.stream(
        "GET",
        "/agents/alpha/tail",
        params={"since": "2026-05-01T00:00:00"},
        headers=_auth_extra(),
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")
    assert '"old"' not in body
    assert '"new"' in body
    assert "no-ts-after-cross" in body


# --- agent_card OSError ---------------------------------------------------


def test_card_yaml_oserror_is_500(client_extra, tmp_path):
    c, _ = client_extra
    # Force open() to raise OSError when called inside agent_card.
    real_open = open

    def bad_open(path, *a, **kw):
        if str(path).endswith("spec.yaml"):
            raise OSError("disk burst")
        return real_open(path, *a, **kw)

    with patch("builtins.open", side_effect=bad_open):
        r = c.get("/agents/alpha/card", headers=_auth_extra())
    assert r.status_code == 500
    assert "disk burst" in r.json()["error"]


# --- agent_delete OSError -------------------------------------------------


def test_delete_oserror_is_500(client_extra, tmp_path):
    c, _ = client_extra
    (tmp_path / "state" / "alpha" / "pid").write_text("42", encoding="utf-8")
    with patch(
        "scitex_agent_container._listen.server.os.kill",
        side_effect=OSError("ESRCH"),
    ):
        r = c.delete("/agents/alpha", headers=_auth_extra())
    assert r.status_code == 500
    assert "ESRCH" in r.json()["error"]


def test_delete_bad_pid_value_is_500(client_extra, tmp_path):
    c, _ = client_extra
    (tmp_path / "state" / "alpha" / "pid").write_text("not-an-int", encoding="utf-8")
    r = c.delete("/agents/alpha", headers=_auth_extra())
    assert r.status_code == 500


# --- _sse_frame -----------------------------------------------------------


def test_sse_frame_with_and_without_event():
    assert _sse_frame("ev", "hi") == b"event: ev\ndata: hi\n\n"
    assert _sse_frame(None, "hi") == b"data: hi\n\n"
