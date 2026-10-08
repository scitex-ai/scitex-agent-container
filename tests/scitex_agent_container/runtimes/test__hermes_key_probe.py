"""Real HTTP probes drive the Hermes key preflight without mocks."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scitex_agent_container.runtimes import _hermes_key_probe as probe
from scitex_agent_container.runtimes._hermes_failover import DeclaredPool


@contextmanager
def backend(statuses):
    """Serve canned per-token verdicts over real HTTP on loopback."""
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            key = self.headers.get("Authorization", "").removeprefix("Bearer ")
            observed.append((self.path, key))
            status = statuses.get(key, 401)
            if status == 200:
                body = {
                    "id": "probe-ok",
                    "model": payload.get("model"),
                    "choices": [{"message": {"content": "OK"}}],
                }
            elif status == 429:
                body = {
                    "error": {
                        "message": "usage limit exceeded",
                        "reset_at": time.time() + 3600,
                    }
                }
            else:
                body = {"error": {"message": "Invalid Authorization header"}}
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


def _probe(url, name):
    return {
        "url": url,
        "protocol": "openai-chat-completions",
        "model": "model-" + name,
        "provider": name,
        "session_id": "diagnostic",
        "headers": {},
        "model_config": {"provider": name, "default": "model-" + name},
    }


def _pool(name, url, tokens):
    rows = [
        {
            "id": f"{name}-{index}",
            "label": f"KEY_{index}",
            "source": f"manual:sac:KEY_{index}",
            "auth_type": "api_key",
            "priority": index,
            "access_token": token,
        }
        for index, token in enumerate(tokens)
    ]
    return DeclaredPool(rows, [], _probe(url, name))


def _rendered(name, fallbacks=()):
    return {
        "model": {"provider": name, "default": "model-" + name},
        "fallback_providers": [
            {"provider": other, "model": "model-" + other} for other in fallbacks
        ],
    }


def _rows(state_dir, provider):
    return json.loads((state_dir / "auth.json").read_text())["credential_pool"][
        provider
    ]


def test_rejected_key_is_marked_dead_while_healthy_key_passes(tmp_path):
    # Arrange
    with backend({"fake-dead": 401, "fake-ok": 200}) as (url, _observed):
        pools = {"go": _pool("go", url, ["fake-dead", "fake-ok"])}
        rendered = _rendered("go")
        # Act
        probe.preflight_pools(rendered, pools, [tmp_path])
    # Assert
    assert [row["last_status"] for row in _rows(tmp_path, "go")] == ["dead", "ok"]


def test_dead_key_is_not_reprobed_on_the_next_preflight(tmp_path):
    # Arrange
    with backend({"fake-dead": 401, "fake-ok": 200}) as (url, observed):
        pools = {"go": _pool("go", url, ["fake-dead", "fake-ok"])}
        rendered = _rendered("go")
        probe.preflight_pools(rendered, pools, [tmp_path])
        # Act
        observed.clear()
        probe.preflight_pools(rendered, pools, [tmp_path])
    # Assert
    assert [key for _, key in observed] == ["fake-ok"]


def test_unavailable_primary_falls_back_to_verified_backup(tmp_path):
    # Arrange
    with backend({"fake-go": 401, "fake-backup": 200}) as (url, _observed):
        pools = {
            "go": _pool("go", url, ["fake-go"]),
            "backup": _pool("backup", url, ["fake-backup"]),
        }
        rendered = _rendered("go", fallbacks=["backup"])
        # Act
        probe.preflight_pools(rendered, pools, [tmp_path])
    # Assert
    assert (rendered["model"], rendered["fallback_providers"]) == (
        {"provider": "backup", "default": "model-backup"},
        [{"provider": "go", "model": "model-go"}],
    )


def test_home_and_overlay_backings_carry_identical_bytes(tmp_path):
    # Arrange
    with backend({"fake-ok": 200}) as (url, _observed):
        pools = {"go": _pool("go", url, ["fake-ok"])}
        rendered = _rendered("go")
        # Act
        probe.preflight_pools(rendered, pools, [tmp_path / "home", tmp_path / "overlay"])
    # Assert
    assert (tmp_path / "home/auth.json").read_bytes() == (
        tmp_path / "overlay/auth.json"
    ).read_bytes()


def test_quota_exhausted_key_is_skipped_until_its_reset(tmp_path):
    # Arrange
    with backend({"fake-quota": 429, "fake-ok": 200}) as (url, observed):
        pool = _pool("go", url, ["fake-quota", "fake-ok"])
        pool.credentials[0].update(
            last_status="exhausted",
            last_error_code=429,
            last_status_at=time.time() - 10,
            last_error_reset_at=time.time() + 3600,
        )
        pools = {"go": pool}
        rendered = _rendered("go")
        # Act
        probe.preflight_pools(rendered, pools, [tmp_path])
    # Assert
    assert [key for _, key in observed] == ["fake-ok"]


def test_quota_key_is_retried_once_its_reset_passes(tmp_path):
    # Arrange
    with backend({"fake-quota": 429, "fake-ok": 200}) as (url, observed):
        pool = _pool("go", url, ["fake-quota", "fake-ok"])
        pool.credentials[0].update(
            last_status="exhausted",
            last_error_code=429,
            last_status_at=time.time() - 7200,
            last_error_reset_at=time.time() - 10,
        )
        pools = {"go": pool}
        rendered = _rendered("go")
        # Act
        probe.preflight_pools(rendered, pools, [tmp_path])
    # Assert
    assert [key for _, key in observed] == ["fake-quota", "fake-ok"]


def test_all_failed_refuses_launch(tmp_path):
    # Arrange
    with backend({"fake-dead-a": 401, "fake-dead-b": 401}) as (url, _observed):
        pools = {"go": _pool("go", url, ["fake-dead-a", "fake-dead-b"])}
        rendered = _rendered("go")
        # Act
        call = lambda: probe.preflight_pools(rendered, pools, [tmp_path])  # noqa: E731
        # Assert
        with pytest.raises(RuntimeError, match="No declared Hermes account passed"):
            call()


def test_all_failed_keeps_declared_keys(tmp_path):
    # Arrange
    with backend({"fake-dead-a": 401, "fake-dead-b": 401}) as (url, _observed):
        pools = {"go": _pool("go", url, ["fake-dead-a", "fake-dead-b"])}
        rendered = _rendered("go")
        # Act
        try:
            probe.preflight_pools(rendered, pools, [tmp_path])
        except RuntimeError:
            refused = True
        else:
            refused = False
    # Assert
    assert (refused, len(_rows(tmp_path, "go"))) == (True, 2)
