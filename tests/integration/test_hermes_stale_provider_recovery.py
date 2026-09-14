"""Synthetic adapter integration for stale-provider recovery.

This exercises real HTTP health/completion boundaries with a behavioral fake
session. It does not prove the pinned Hermes TUI path in a production runtime.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from scitex_agent_container.runtimes import _hermes_stale_recovery as recovery


class _Gateway(ThreadingHTTPServer):
    requests: list[str]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.server.requests.append(self.path)
        self._json(
            {
                "status": "ok",
                "members": [
                    {"active": True, "in_flight": 0, "queued": 0, "capacity": 1}
                ],
            }
        )

    def do_POST(self):  # noqa: N802
        self.server.requests.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._json({"choices": [{"message": {"content": "resumed"}}]})


def test_stale_latch_recovers_and_http_turn_keeps_all_identities(tmp_path):
    # Arrange
    gateway = _Gateway(("127.0.0.1", 0), _Handler)
    gateway.requests = []
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{gateway.server_port}"
    config = SimpleNamespace(
        name="scholar",
        model="qwen38-27b",
        engine_key="qwen38-27b",
        claude=SimpleNamespace(provider=SimpleNamespace(base_url=f"{base}/v1")),
    )
    state_dir = tmp_path / "agent"
    identities = {
        "session_id": "hermes-session-unchanged",
        "instance_id": "sac-incarnation-unchanged",
        # Hermes-private session/context state; not the SciTeX shared store.
        "home/.hermes/state.db": "context-unchanged",
    }
    for relative, body in identities.items():
        path = state_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    session = {
        "latched": True,
        "pane": "Provider has been unresponsive for 5 consecutive stale attempts",
        "commands": [],
    }

    def same_session_rebind():
        session["commands"].append(recovery.recovery_command(config))
        session["commands"].append("/heartbeat resume")
        session["latched"] = False
        return True

    try:
        # Act: the monitor proves capacity and resets Hermes in place.
        latched = recovery.recovery_tick(
            config,
            capture=lambda: session["pane"],
            pause=lambda: session["commands"].append("/heartbeat pause") or True,
            recover=same_session_rebind,
            state_dir=state_dir,
        )
        recovery.recovery_tick(
            config,
            capture=lambda: session["pane"],
            pause=lambda: False,
            recover=same_session_rebind,
            previous_fingerprint=latched,
            state_dir=state_dir,
        )
        # The next autonomous turn now reaches the configured inference API.
        request = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=b'{"model":"qwen38-27b","messages":[]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            completion = json.loads(response.read())
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=2)

    # Assert: health was the only probe, then one real turn; no restart/new
    # session/context deletion occurred anywhere in the recovery path.
    assert (
        gateway.requests,
        session["latched"],
        session["commands"],
        completion["choices"][0]["message"]["content"],
        {key: (state_dir / key).read_text(encoding="utf-8") for key in identities},
    ) == (
        ["/health", "/v1/chat/completions"],
        False,
        [
            "/heartbeat pause",
            "/model qwen38-27b --provider custom:sac-qwen38-27b --session",
            "/heartbeat resume",
        ],
        "resumed",
        identities,
    )
