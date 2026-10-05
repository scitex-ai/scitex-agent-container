"""An actual rejected HTTP probe must leave deployed configuration intact."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.runtimes._hermes_profile import (
    materialize_hermes_tui_profile,
)


def test_unavailable_key_preserves_existing_profile(tmp_path, monkeypatch):
    # Arrange
    class Reject(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"error":{"message":"Invalid Authorization header"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Reject)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = AgentConfig(
        name="availability-diagnostic",
        harness="hermes",
        runtime="tui",
        workdir=str(tmp_path),
    )
    config.engine_key, config.model = "diagnostic", "diagnostic-model"
    config.claude.provider = ProviderSpec(
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        auth_token_env="SAC_DIAGNOSTIC_KEY",
    )
    config.hermes_failover = HermesFailoverSpec(
        accounts={"diagnostic": ["SAC_DIAGNOSTIC_KEY"]}
    )
    monkeypatch.setenv("SAC_DIAGNOSTIC_KEY", "fake-rejected-key")
    existing = tmp_path / "runtime/home/.hermes/config.yaml"
    existing.parent.mkdir(parents=True)
    original = b"model: {default: original-model, provider: original-provider}\n"
    existing.write_bytes(original)
    # Act
    refused = False
    try:
        materialize_hermes_tui_profile(config, state_dir=tmp_path / "runtime")
    except RuntimeError as error:
        refused = "No declared Hermes account passed" in str(error)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    # Assert
    assert (refused, existing.read_bytes()) == (True, original)
