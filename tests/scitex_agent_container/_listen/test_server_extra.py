"""Additional coverage for ``scitex_agent_container._listen.server``.

Targets gaps left by ``test_server.py``:
  - ``_find_claude_binary`` bundled hit + PATH miss.
  - ``list_agents`` registry-error 500.
  - ``_forward_to_live_runner`` HTTPError → propagated status.
  - ``agent_send`` unknown ``type``, OSError in key path, key path
    without live-session pid, claude binary missing.
  - ``_stream_claude`` FileNotFoundError + cancellation.
  - ``_parse_iso_ts`` / ``_record_ts`` direct unit calls.
  - ``_runtime_session_jsonl`` default home path.
  - ``agent_tail`` non-follow 404, line streaming, since filter,
    malformed-line fallback.
  - ``agent_card`` OSError when YAML file is unreadable.
  - ``agent_delete`` OSError surfaces as 500.
"""

from __future__ import annotations

import asyncio
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import (
    _find_claude_binary,
    _parse_iso_ts,
    _record_ts,
    _runtime_session_jsonl,
    _sse_frame,
    _stream_claude,
    create_app,
)

TOKEN = "test-token-1234567890"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _seed_spec(
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
def client(tmp_path, monkeypatch):
    _seed_spec(tmp_path, "alpha")
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


def test_list_agents_returns_500_on_registry_error(client):
    c, _ = client

    class Boom:
        def list_all(self):
            raise RuntimeError("registry broken")

    with patch(
        "scitex_agent_container._listen.server.Registry", side_effect=lambda: Boom()
    ):
        r = c.get("/v1/agents", headers=_auth())
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
        r = c.post("/v1/agents/live/send", json={"prompt": "x"}, headers=_auth())
    assert r.status_code == 502
    body = r.json()
    assert body["route"] == "live-runner"
    assert body["status"] == 502
    assert "upstream" in body["error"]


# --- agent_send unknown type ----------------------------------------------


def test_send_unknown_type_is_400(client):
    c, _ = client
    r = c.post(
        "/v1/agents/alpha/send",
        json={"type": "weird"},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert "unknown type" in r.json()["error"]


def test_send_key_no_live_session_is_404(client):
    c, _ = client
    # no pid file seeded for alpha
    r = c.post(
        "/v1/agents/alpha/send", json={"type": "key", "key": "C-c"}, headers=_auth()
    )
    assert r.status_code == 404
    assert "no live session" in r.json()["error"]


def test_send_key_oskill_oserror_is_500(client):
    c, tmp_path = client
    (tmp_path / "state" / "alpha" / "pid").write_text("777", encoding="utf-8")

    def boom(pid, sig):
        raise OSError("no such process")

    with patch("scitex_agent_container._listen.server.os.kill", side_effect=boom):
        r = c.post(
            "/v1/agents/alpha/send", json={"type": "key", "key": "ESC"}, headers=_auth()
        )
    assert r.status_code == 500
    assert "no such process" in r.json()["error"]


def test_send_claude_binary_missing_is_500(client):
    c, _ = client
    with patch(
        "scitex_agent_container._listen.server._find_claude_binary",
        side_effect=RuntimeError("claude binary not found"),
    ):
        r = c.post("/v1/agents/alpha/send", json={"prompt": "hi"}, headers=_auth())
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


def test_tail_404_when_missing_and_not_follow(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setattr(
        "scitex_agent_container._listen.server._runtime_session_jsonl",
        lambda name: tmp_path / "missing.jsonl",
    )
    r = c.get("/v1/agents/alpha/tail", headers=_auth())
    assert r.status_code == 404
    assert "no session.jsonl" in r.json()["error"]


def test_tail_streams_existing_lines(client, tmp_path, monkeypatch):
    c, _ = client
    jsonl = tmp_path / "alpha.jsonl"
    jsonl.write_text(
        '{"ts":"2026-05-01T00:00:00","msg":"a"}\n'
        "\n"  # empty line skipped
        "not-json-{{\n"  # malformed → {"raw": ...}
        '{"ts":"2026-05-01T01:00:00","msg":"b"}\n'
    )
    monkeypatch.setattr(
        "scitex_agent_container._listen.server._runtime_session_jsonl",
        lambda name: jsonl,
    )
    with c.stream("GET", "/v1/agents/alpha/tail", headers=_auth()) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode("utf-8")
    assert '"msg": "a"' in body
    assert '"msg": "b"' in body
    assert '"raw"' in body  # malformed line surfaced


def test_tail_since_filter_drops_old_records(client, tmp_path, monkeypatch):
    c, _ = client
    jsonl = tmp_path / "alpha.jsonl"
    jsonl.write_text(
        '{"ts":"2026-01-01T00:00:00","msg":"old"}\n'
        '{"ts":"2026-06-01T00:00:00","msg":"new"}\n'
        '{"msg":"no-ts-after-cross"}\n'  # included once we've crossed
    )
    monkeypatch.setattr(
        "scitex_agent_container._listen.server._runtime_session_jsonl",
        lambda name: jsonl,
    )
    with c.stream(
        "GET",
        "/v1/agents/alpha/tail",
        params={"since": "2026-05-01T00:00:00"},
        headers=_auth(),
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")
    assert '"old"' not in body
    assert '"new"' in body
    assert "no-ts-after-cross" in body


# --- agent_card OSError ---------------------------------------------------


def test_card_yaml_oserror_is_500(client, tmp_path):
    c, _ = client
    # Force open() to raise OSError when called inside agent_card.
    real_open = open

    def bad_open(path, *a, **kw):
        if str(path).endswith("spec.yaml"):
            raise OSError("disk burst")
        return real_open(path, *a, **kw)

    with patch("builtins.open", side_effect=bad_open):
        r = c.get("/v1/agents/alpha/card", headers=_auth())
    assert r.status_code == 500
    assert "disk burst" in r.json()["error"]


# --- agent_delete OSError -------------------------------------------------


def test_delete_oserror_is_500(client, tmp_path):
    c, _ = client
    (tmp_path / "state" / "alpha" / "pid").write_text("42", encoding="utf-8")
    with patch(
        "scitex_agent_container._listen.server.os.kill",
        side_effect=OSError("ESRCH"),
    ):
        r = c.delete("/v1/agents/alpha", headers=_auth())
    assert r.status_code == 500
    assert "ESRCH" in r.json()["error"]


def test_delete_bad_pid_value_is_500(client, tmp_path):
    c, _ = client
    (tmp_path / "state" / "alpha" / "pid").write_text("not-an-int", encoding="utf-8")
    r = c.delete("/v1/agents/alpha", headers=_auth())
    assert r.status_code == 500


# --- _sse_frame -----------------------------------------------------------


def test_sse_frame_with_and_without_event():
    assert _sse_frame("ev", "hi") == b"event: ev\ndata: hi\n\n"
    assert _sse_frame(None, "hi") == b"data: hi\n\n"
