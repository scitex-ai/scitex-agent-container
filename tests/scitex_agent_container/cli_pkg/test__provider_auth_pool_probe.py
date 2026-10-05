"""Real HTTP checks follow the declared keys and preserve the auth control."""

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scitex_agent_container.cli_pkg._provider_auth_probe import (
    INDISCRIMINATE,
    OK,
    PROBE_FAILED,
    UNREACHABLE,
    probe_provider_auth,
)
from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._engine_types import EngineSpec
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec


@contextmanager
def backend(statuses, *, accept_control=False):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers.get("Authorization", "").removeprefix("Bearer ")
            observed.append((self.path, key))
            status = statuses.get(key, 200 if accept_control else 401)
            body = (
                {
                    "model": payload["model"],
                    "choices": [{"message": {"content": "OK"}}],
                    "output": [],
                    "status": "completed",
                }
                if status == 200
                else {"error": {"message": "Synthetic rejection"}}
            )
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def config_for(url, monkeypatch, *, keys, backup=False):
    config = AgentConfig(
        name="pool-diagnostic", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key, config.model = "primary", "muse-spark-1.3-contributor"
    config.claude.provider = ProviderSpec(
        base_url=url + "/v1/responses", auth_token_env="POOL_BASE"
    )
    monkeypatch.setenv("POOL_BASE", "fake-ambient-good")
    names = []
    for i, token in enumerate(keys):
        name = f"SAC_POOL_CHECK_{i}"
        monkeypatch.setenv(name, token)
        names.append(name)
    config.hermes_failover = HermesFailoverSpec(accounts={"primary": names})
    if backup:
        monkeypatch.setenv("SAC_POOL_BACKUP", "fake-backup-good")
        config.engines["backup"] = EngineSpec(
            key="backup",
            model="meta/muse-spark-1.3-contributor",
            provider=ProviderSpec(
                base_url=url + "/backup/v1", auth_token_env="SAC_POOL_BACKUP"
            ),
        )
        config.hermes_failover.engines = ["backup"]
        config.hermes_failover.accounts["backup"] = ["SAC_POOL_BACKUP"]
    return config


def test_check_skips_rejected_and_quota_keys_without_editing_spec(monkeypatch):
    # Arrange
    with backend({"fake-bad": 401, "fake-quota": 429, "fake-good": 200}) as (
        url,
        observed,
    ):
        config = config_for(
            url, monkeypatch, keys=["fake-bad", "fake-quota", "fake-good"]
        )
        # Act
        verdict = probe_provider_auth(config)
    # Assert
    assert (
        verdict.state,
        [key for _, key in observed],
        config.claude.provider.auth_token_env,
        len(config.hermes_failover.accounts["primary"]),
    ) == (
        OK,
        [
            "fake-bad",
            "fake-quota",
            "fake-good",
            "fake-good",
            "sac-preflight-control-not-a-valid-key",
        ],
        "POOL_BASE",
        3,
    )


def test_check_cascades_to_declared_backup_provider(monkeypatch):
    # Arrange
    with backend({"fake-quota": 429, "fake-backup-good": 200}) as (url, observed):
        config = config_for(url, monkeypatch, keys=["fake-quota"], backup=True)
        # Act
        verdict = probe_provider_auth(config)
    # Assert
    assert (verdict.state, verdict.actual_model, [path for path, _ in observed]) == (
        OK,
        "meta/muse-spark-1.3-contributor",
        [
            "/v1/responses",
            "/backup/v1/chat/completions",
            "/backup/v1/chat/completions",
            "/backup/v1/chat/completions",
        ],
    )


def test_pool_check_still_rejects_an_indiscriminate_auth_surface(monkeypatch):
    # Arrange
    with backend({"fake-good": 200}, accept_control=True) as (url, observed):
        config = config_for(url, monkeypatch, keys=["fake-good"])
        # Act
        verdict = probe_provider_auth(config)
    # Assert
    assert (verdict.state, verdict.is_failure, len(observed)) == (
        INDISCRIMINATE,
        True,
        3,
    )


def test_pool_check_cannot_use_an_undeclared_working_alias(monkeypatch):
    # Arrange
    with backend({"fake-bad": 401, "fake-ambient-good": 200}) as (url, observed):
        config = config_for(url, monkeypatch, keys=["fake-bad"])
        # Act
        verdict = probe_provider_auth(config)
    # Assert
    assert (verdict.state, verdict.is_failure, observed) == (
        PROBE_FAILED,
        True,
        [("/v1/responses", "fake-bad")],
    )


def test_pool_transport_failure_remains_unknown(monkeypatch):
    # Arrange
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = config_for(url, monkeypatch, keys=["fake-good"])
    # Act
    verdict = probe_provider_auth(config, timeout=0.1)
    # Assert
    assert (verdict.state, verdict.is_failure) == (UNREACHABLE, False)
