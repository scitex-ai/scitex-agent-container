"""Probe real HTTP transport; HTTP 200 can still contain failed inference."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from scitex_agent_container.runtimes._availability import probe_provider_key


@pytest.mark.parametrize("payload,available", [
    ({"status": "completed", "output": [{"type": "message"}]}, True),
    ({"status": "failed", "error": {"message": "model unavailable"}}, False),
    ({"error": {"message": "quota exhausted"}}, False),
])
def test_http_success_does_not_hide_failed_inference(payload, available):
    # Arrange
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Act
        result = probe_provider_key(
            "custom:test", "example-model", "synthetic-key",
            endpoint_url=f"http://127.0.0.1:{server.server_port}/v1/responses",
            protocol="openai-responses", timeout_s=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    # Assert
    assert result.available is available
