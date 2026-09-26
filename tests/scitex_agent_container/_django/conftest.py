"""Shared fixtures for the Agents dashboard (``_django``) test suite.

No mocks, no ``monkeypatch`` (STX-NM002 / PA-306 §3). The view layer is tested
against a REAL loopback HTTP listener that serves the exact SAC control-plane
contract (``GET /agents``, ``GET /agents/<name>/status``, ``GET /agents/<name>/
tail``), so the transport is real end to end — the same honest-stand-in pattern
as ``ssh_http_shim``. Env is set through the shared function-scoped
``env_save_restore`` fixture (auto-restored), never ``monkeypatch``.

Run from the worktree with the worktree ``src`` on the path::

    DJANGO_SETTINGS_MODULE=scitex_agent_container._django._test_settings \
    PYTHONPATH="$PWD/src" pytest tests/scitex_agent_container/_django
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

# Force the in-package Django settings for this suite (independent of the repo's
# own conftest floors, which sandbox a different concern).
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "scitex_agent_container._django._test_settings"
)

import django  # noqa: E402

django.setup()

from django.test import Client, override_settings  # noqa: E402

# Node names for the scope test: this node is own-scope; the other is cross-host.
# The row's node is read from ``turn_url``'s hostname (the authoritative signal).
LOCAL_NAME = socket.gethostname() or "this-node"
REMOTE_NAME = "remote-node-" + (LOCAL_NAME[:8] or "x")

# The bearer token the loopback listener requires.
TOKEN = "test-loopback-token"

# ── Deterministic control-plane data (the shape the real listener returns) ──────
AGENTS: list[dict[str, Any]] = [
    {"name": "alpha", "role": "worker", "project": "proj-a", "pid": 111, "a2a_port": 19000,
     "turn_url": f"http://{LOCAL_NAME}:19000/v1/turn", "started_at": "2026-09-01T00:00:00Z"},
    {"name": "beta", "role": "worker", "project": "proj-b", "pid": 222, "a2a_port": 19001,
     "turn_url": f"http://{LOCAL_NAME}:19001/v1/turn", "started_at": "2026-09-01T00:00:00Z"},
    {"name": "gamma", "role": "worker", "project": "proj-g", "pid": 333, "a2a_port": 19002,
     "turn_url": f"http://{REMOTE_NAME}:19002/v1/turn", "started_at": "2026-09-01T00:00:00Z"},
    {"name": "delta", "role": "worker", "project": "proj-d", "pid": 444, "a2a_port": 19003,
     "turn_url": f"http://{LOCAL_NAME}:19003/v1/turn", "started_at": "2026-09-01T00:00:00Z"},
]

STATUS: dict[str, Any] = {
    "alpha": {"name": "alpha", "liveness": {"verdict": "ALIVE"}, "status": "running",
              "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
              "model": "sonnet", "billing_mode": "subscription",
              "auth_identity": "anthropic/team-max", "runtime_identity_source": "birth_certificate",
              "pid": 4294967291, "session_id": "a" * 32,
              "a2a_port": 19000, "turn_url": f"http://{LOCAL_NAME}:19000/v1/turn",
              "inbox_reachable": "true",
              "activity": {
                  "operation": {"state": "observed", "value": "busy", "source": "heartbeat.state"},
                  "phase": {"state": "observed", "value": "reviewing", "source": "heartbeat.current_phase"},
              }},
    "beta": {"name": "beta", "liveness": {"verdict": "DEAD"}, "status": "stopped",
             "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
             "model": "haiku", "pid": 222, "session_id": "b" * 32,
             "a2a_port": 19001, "turn_url": f"http://{LOCAL_NAME}:19001/v1/turn",
             "inbox_reachable": "unknown"},
    "gamma": {"name": "gamma", "liveness": {"verdict": "ALIVE"}, "status": "running",
              "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
              "model": "sonnet", "pid": 333, "session_id": "c" * 32,
              "a2a_port": 19002, "turn_url": f"http://{REMOTE_NAME}:19002/v1/turn",
              "inbox_reachable": "true"},
}

# /status for delta returns a TYPED 400 (spec invalid), not a body.
SPEC_ERROR = {"error": "Config validation failed for delta/spec.yaml",
              "kind": "spec_resolution_failed", "name": "delta"}

# SSE tail for alpha (follow=false). Carries a secret to prove redaction.
TAIL_ALPHA = (
    'data: {"line_no": 1, "record": {"type": "user", "text": "start the job"}}\n\n'
    'data: {"line_no": 2, "record": {"type": "assistant", '
    '"text": "done; token sk-abc123DEF456GHI789jkl012 is set"}}\n\n'
    'data: {"line_no": 3, "record": {"type": "result", "text": "finished ok"}}\n'
)


class _Listener(BaseHTTPRequestHandler):
    """Serves the SAC control-plane contract with bearer auth (real HTTP)."""

    request_paths: list[str] = []
    request_bodies: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        return  # silence request logging in tests

    def _bearer_ok(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        type(self).request_paths.append(self.path.split("?", 1)[0])
        if not self._bearer_ok():
            self._send(401, b'{"error": "missing bearer token"}')
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/agents":
            # The real listener batches status/runtime identity into this row;
            # the dashboard must not fan out to /status per agent.
            agents = [{**row, **STATUS.get(str(row.get("name")), {})} for row in AGENTS]
            self._send(200, json.dumps({"agents": agents}).encode())
        elif path.endswith("/status"):
            name = path.rsplit("/status", 1)[0].rsplit("/", 1)[-1]
            if name == "delta":
                self._send(400, json.dumps(SPEC_ERROR).encode())
            elif name in STATUS:
                self._send(200, json.dumps(STATUS[name]).encode())
            else:
                self._send(404, b'{"error": "unknown agent", "kind": "unknown_agent"}')
        elif path.endswith("/tail"):
            name = path.rsplit("/tail", 1)[0].rsplit("/", 1)[-1]
            if name == "alpha":
                self._send(200, TAIL_ALPHA.encode(), "text/event-stream")
            else:
                self._send(404, json.dumps({"error": f"no session.jsonl for {name!r}"}).encode())
        else:
            self._send(404, b'{"error": "not found"}')

    def _read_json_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {}

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        type(self).request_paths.append(f"POST {path}")
        if not self._bearer_ok():
            self._send(401, b'{"error": "missing bearer token"}')
            return
        body = self._read_json_body()
        type(self).request_bodies.append({"path": path, "body": body})
        stripped = path.rstrip("/")
        if stripped == "/agents":
            # Shape-1 start of a pre-registered spec; unknown names are a
            # typed 404, exactly like the real listener.
            name = body.get("name") if isinstance(body, dict) else None
            known = {row["name"] for row in AGENTS} | {"epsilon"}
            if name in known:
                self._send(200, json.dumps({"status": "started", "name": name}).encode())
            else:
                self._send(404, b'{"error": "unknown agent", "kind": "unknown_agent"}')
        elif stripped.endswith("/restart"):
            self._send(200, json.dumps({"status": "restarted"}).encode())
        elif stripped.endswith("/send"):
            self._send(200, json.dumps({"delivered": True}).encode())
        else:
            self._send(404, b'{"error": "not found"}')

    def do_DELETE(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        type(self).request_paths.append(f"DELETE {path}")
        if not self._bearer_ok():
            self._send(401, b'{"error": "missing bearer token"}')
            return
        if path.rstrip("/").startswith("/agents/"):
            self._send(200, json.dumps({"status": "stopped"}).encode())
        else:
            self._send(404, b'{"error": "not found"}')


@pytest.fixture
def loopback(env_save_restore):
    """Start a real loopback listener serving the contract; point the app at it.

    Yields the port. ``env_save_restore`` (auto-restoring) sets the API URL +
    token, so the view's ``RemoteFleet.from_environment()`` reaches this server
    over real HTTP with no patching.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Listener)
    _Listener.request_paths.clear()
    _Listener.request_bodies.clear()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", f"http://127.0.0.1:{port}")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_TOKEN", TOKEN)
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def listener_requests(loopback):
    """Paths observed by the real loopback listener for fan-out assertions."""
    _Listener.request_paths.clear()
    return _Listener.request_paths


@pytest.fixture
def listener_posts(loopback):
    """POST/DELETE bodies observed by the loopback listener, in order."""
    _Listener.request_bodies.clear()
    return _Listener.request_bodies


@pytest.fixture
def unreachable_listener(env_save_restore):
    """Point the app at a closed port with no token: an unreachable listener."""
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_API_TOKEN")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_API_URL", "http://127.0.0.1:1")
    yield


@pytest.fixture
def audit_log(env_save_restore, tmp_path):
    """Route the cross-host audit trail to a tmp file (auto-restored)."""
    path = tmp_path / "audit.log"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_GUI_AUDIT_LOG", str(path))
    return path


@pytest.fixture
def hub_client(tmp_path, env_save_restore):
    """A client whose urlconf mounts the app under /apps/agents/ and whose
    global_base.html is a minimal stub proving content lands in the Hub shell
    (not the standalone shell). Uses override_settings + a tmp test urlconf —
    no monkeypatch."""
    templates_dir = tmp_path / "hub_templates"
    templates_dir.mkdir()
    (templates_dir / "global_base.html").write_text(
        '<html><head>{% block head_extra %}{% endblock %}{% block extra_css %}{% endblock %}'
        '</head><body><div id="hub-global-header">HUB-SHELL</div>'
        '{% block content %}{% endblock %}</body></html>',
        encoding="utf-8",
    )
    test_urls = tmp_path / "hub_test_urls.py"
    test_urls.write_text(
        "from django.urls import include, path\n"
        "urlpatterns = [path('apps/agents/', include('scitex_agent_container._django.urls'))]\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        with override_settings(
            TEMPLATES=[{
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "DIRS": [str(templates_dir)],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
            }],
            ROOT_URLCONF="hub_test_urls",
        ):
            yield Client()
    finally:
        sys.path.remove(str(tmp_path))


@pytest.fixture
def client():
    return Client()


# ── B6: the shared global CACHE must not leak state between tests ────────────
# The views read/write the process-wide ``_inventory_cache.CACHE``. A snapshot
# (or a recorded error) left by one test is visible to the next, so a test can
# PASS on state it did not create (or a permissive "loading-or-unavailable"
# assertion can mask a real defect). The suite's own ``isolated_cache`` fixture
# swapped the module global, but it is opt-in; an autouse floor is the B6 fix -
# every _django test starts from a clean cache, exactly like the conftest floors
# for the state root and the event log.
@pytest.fixture(autouse=True)
def _isolate_global_cache():
    from scitex_agent_container._django import _inventory_cache as _module

    original = _module.CACHE
    _module.CACHE = _module.InventoryCache()
    try:
        yield _module.CACHE
    finally:
        _module.CACHE = original
