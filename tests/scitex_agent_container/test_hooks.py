"""Tests for hooks.run_hook and lifecycle wiring (todo#286 Phase 4).

No-mocks rewrite: every test exercises real production code with real
collaborators — real ``subprocess.run``, real ``urllib`` HTTP client
talking to a real localhost server, real ``ThreadPoolExecutor``, real
``AgentConfig`` loaded from a real YAML file, real ``Registry``, real
``ClaudeSessionRuntime`` (whose ``is_running`` honestly returns False
when no apptainer PID file exists). The only test-only seam is
``hooks.run_hook(..., pool=...)`` which accepts an injected, joinable
executor so we can block on completion instead of polling — its
default value is the production-shared ``_POOL`` and behaviour is
otherwise identical.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scitex_agent_container import hooks
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_recording_shim(
    subprocess_shim, name: str, *, exit_code: int = 0, sleep_s: float = 0.0
) -> Path:
    """Install a fake binary that records argv + selected env vars to a log.

    Returns the path of the JSONL log file. Each invocation appends one
    JSON object capturing argv tail and the SCITEX-related env vars we
    care about asserting on.
    """
    log = subprocess_shim._bin / f"{name}.invocations.jsonl"
    script = subprocess_shim._bin / name
    body = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        f"time.sleep({float(sleep_s)})\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'sac_name': os.environ.get('SAC_NAME'),\n"
        "        'scitex_hook': os.environ.get('SCITEX_HOOK'),\n"
        "        'ctx_k': os.environ.get('SCITEX_HOOK_CTX_K'),\n"
        "    }) + '\\n')\n"
        f"sys.exit({int(exit_code)})\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return log


def _wait_for_log(log: Path, timeout_s: float = 5.0) -> list[dict]:
    """Block until ``log`` exists and contains at least one line, then return all."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.02)
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
def joinable_pool():
    """Real ThreadPoolExecutor that the test can shutdown(wait=True)."""
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scitex-hook-test")
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)


# ---------------------------------------------------------------------------
# run_hook — shell dispatch (real subprocess via PATH shim)
# ---------------------------------------------------------------------------


def test_run_hook_shell_runs_real_command(subprocess_shim, joinable_pool):
    # Arrange
    log = _install_recording_shim(subprocess_shim, "sac_hook_echo")
    # Act
    hooks.run_hook(
        "agent-x", "pre_start", ["sac_hook_echo arg1 arg2"], pool=joinable_pool
    )
    joinable_pool.shutdown(wait=True)
    # Assert
    assert [inv["argv"] for inv in _wait_for_log(log)] == [["arg1", "arg2"]]


def test_run_hook_shell_passes_sac_name_env(subprocess_shim, joinable_pool):
    # Arrange
    log = _install_recording_shim(subprocess_shim, "sac_hook_envcheck")
    # Act
    hooks.run_hook("agent-x", "pre_start", ["sac_hook_envcheck"], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert
    assert _wait_for_log(log)[0]["sac_name"] == "agent-x"


def test_run_hook_shell_passes_hook_name_env(subprocess_shim, joinable_pool):
    # Arrange
    log = _install_recording_shim(subprocess_shim, "sac_hook_envcheck2")
    # Act
    hooks.run_hook("agent-x", "pre_start", ["sac_hook_envcheck2"], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert
    assert _wait_for_log(log)[0]["scitex_hook"] == "pre_start"


def test_run_hook_shell_passes_flattened_context_env(subprocess_shim, joinable_pool):
    # Arrange
    log = _install_recording_shim(subprocess_shim, "sac_hook_ctxcheck")
    # Act
    hooks.run_hook(
        "agent-x",
        "pre_start",
        ["sac_hook_ctxcheck"],
        context={"k": "v"},
        pool=joinable_pool,
    )
    joinable_pool.shutdown(wait=True)
    # Assert
    assert _wait_for_log(log)[0]["ctx_k"] == "v"


def test_run_hook_shell_failure_does_not_raise(subprocess_shim, joinable_pool):
    # Arrange
    _install_recording_shim(subprocess_shim, "sac_hook_fail", exit_code=1)
    # Act
    hooks.run_hook("agent-x", "pre_start", ["sac_hook_fail"], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert — reaching here = swallowed (no exception escaped to the caller)
    assert True


# ---------------------------------------------------------------------------
# run_hook — HTTP dispatch (real localhost server)
# ---------------------------------------------------------------------------


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Thread-shared mailbox lives on the server instance."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.received.append(  # type: ignore[attr-defined]
            {"path": self.path, "method": self.command, "body": body}
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a, **kw):
        return  # silence


@pytest.fixture
def http_capture():
    """Start a real HTTPServer on a free port. Yields (url, server)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.HTTPServer(("127.0.0.1", port), _RecordingHandler)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/hook", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_run_hook_http_posts_payload_with_agent_field(http_capture, joinable_pool):
    # Arrange
    url, server = http_capture
    # Act
    hooks.run_hook(
        "agent-y", "on_compact", [url], context={"percent": 90.0}, pool=joinable_pool
    )
    joinable_pool.shutdown(wait=True)
    # Assert
    payload = json.loads(server.received[0]["body"].decode())
    assert payload["agent"] == "agent-y"


def test_run_hook_http_posts_payload_with_hook_field(http_capture, joinable_pool):
    # Arrange
    url, server = http_capture
    # Act
    hooks.run_hook("agent-y", "on_compact", [url], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert
    payload = json.loads(server.received[0]["body"].decode())
    assert payload["hook"] == "on_compact"


def test_run_hook_http_posts_payload_with_context_field(http_capture, joinable_pool):
    # Arrange
    url, server = http_capture
    # Act
    hooks.run_hook(
        "agent-y", "on_compact", [url], context={"percent": 90.0}, pool=joinable_pool
    )
    joinable_pool.shutdown(wait=True)
    # Assert
    payload = json.loads(server.received[0]["body"].decode())
    assert payload["context"] == {"percent": 90.0}


def test_run_hook_http_uses_post_method(http_capture, joinable_pool):
    # Arrange
    url, server = http_capture
    # Act
    hooks.run_hook("agent-y", "on_compact", [url], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert
    assert server.received[0]["method"] == "POST"


def test_run_hook_http_failure_does_not_raise(joinable_pool):
    # Arrange — port 1 on localhost: nothing listens, connect refused
    unreachable = "http://127.0.0.1:1/hook"
    # Act
    hooks.run_hook("agent-y", "on_compact", [unreachable], pool=joinable_pool)
    joinable_pool.shutdown(wait=True)
    # Assert — reaching here = URLError swallowed
    assert True


# ---------------------------------------------------------------------------
# run_hook — fire-and-forget contract + noop guards
# ---------------------------------------------------------------------------


def test_run_hook_returns_before_slow_command_completes(subprocess_shim, joinable_pool):
    # Arrange — shim sleeps long enough to outlast the call itself
    _install_recording_shim(subprocess_shim, "sac_hook_slow", sleep_s=1.5)
    # Act
    t0 = time.monotonic()
    hooks.run_hook("a", "pre_start", ["sac_hook_slow"], pool=joinable_pool)
    elapsed = time.monotonic() - t0
    # Assert — submit() returns immediately; the 1.5s sleep is in the worker
    assert elapsed < 0.5


def test_run_hook_with_none_commands_is_noop():
    # Arrange — none needed
    # Act
    result = hooks.run_hook("a", "pre_start", None)
    # Assert
    assert result is None


def test_run_hook_with_empty_commands_is_noop():
    # Arrange — none needed
    # Act
    result = hooks.run_hook("a", "pre_start", [])
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Snapshot on_diff wiring (real snapshot_tick, real cache dir, real diff)
# ---------------------------------------------------------------------------


def test_snapshot_on_diff_fires_configured_hook_when_diff_present(
    tmp_path, subprocess_shim, env_save_restore, joinable_pool
):
    # Arrange — point SAC_CACHE_DIR at tmp so snapshot writes locally
    env_save_restore.set("SAC_CACHE_DIR", str(tmp_path))
    from scitex_agent_container._state import snapshot

    # Pre-seed a "latest" snapshot that differs from what the real
    # gather_snapshot will produce — this forces compute_diff_fields to
    # find divergent keys, so the next tick has has_diff=True.
    latest_path = snapshot._latest_path("a3")
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(
        json.dumps(
            {
                "agent": "a3",
                "timestamp": "2000-01-01T00:00:00+00:00",
                "host": "SENTINEL-DIFF-FORCING-HOST",
                "tmux_count": -999,
            }
        )
    )

    log = _install_recording_shim(subprocess_shim, "sac_hook_ondiff")

    # Real AgentConfig — only `.hooks` is consulted by snapshot_tick.
    from scitex_agent_container.config import AgentConfig

    agent_cfg = AgentConfig(name="a3", workdir=str(tmp_path / "work"))
    agent_cfg.hooks = {"on_diff": ["sac_hook_ondiff diffed"]}

    # snapshot_tick imports run_hook lazily and uses the module _POOL,
    # so swap in a real joinable ThreadPoolExecutor for the duration
    # of the call (save/restore brackets it — no global leak). This
    # is a real executor, not a mock.
    original_pool = hooks._POOL
    hooks._POOL = joinable_pool
    try:
        # Act — tick reads seeded "latest" as prev and diffs against fresh
        snapshot.snapshot_tick("a3", agent_config=agent_cfg)
    finally:
        joinable_pool.shutdown(wait=True)
        hooks._POOL = original_pool

    # Assert — shim was invoked with the configured argv tail
    invocations = _wait_for_log(log)
    assert invocations and invocations[-1]["argv"] == ["diffed"]


# ---------------------------------------------------------------------------
# Status --json enrichment (real load_config, real Registry, real runtime)
# ---------------------------------------------------------------------------


def _write_v3_config(tmp_path: Path, extra: str = "") -> Path:
    """v3: dir-as-SSoT — YAML lives at <name>/<name>.yaml, no metadata.name."""
    d = tmp_path / "statustest"
    d.mkdir(exist_ok=True)
    p = d / "statustest.yaml"
    p.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            "  workdir: /home/agent/work\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  health:\n    enabled: true\n    interval: 60\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n"
            "    model: sonnet\n" + extra
        )
    )
    return p


def _real_status(tmp_path: Path, extra: str) -> dict:
    """Drive agent_status end-to-end with real config + registry + runtime.

    The real ``ClaudeSessionRuntime.is_running`` honestly returns False
    because no apptainer PID file exists under the tmp state tree — no
    monkeypatch needed.
    """
    from scitex_agent_container._lifecycle import lifecycle
    from scitex_agent_container._state.registry import Registry
    from scitex_agent_container.config import load_config

    cfg_path = _write_v3_config(tmp_path, extra)
    cfg = load_config(cfg_path)

    registry = Registry(registry_dir=tmp_path / "_registry")
    registry.add(
        name="statustest",
        config_path=str(cfg_path),
        screen_name=cfg.screen_name,
    )
    return lifecycle.agent_status("statustest", registry=registry)


def test_extensions_passthrough_in_status(tmp_path):
    # Arrange
    extra = (
        "  extensions:\n    fleethub:\n      foo: bar\n      nested:\n        a: 1\n"
    )
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["extensions"] == {"fleethub": {"foo": "bar", "nested": {"a": 1}}}


def test_listen_first_entry_port_in_status(tmp_path):
    # Arrange
    extra = (
        "  listen:\n"
        "    - port: 8559\n"
        "      proto: tcp\n"
        "      name: mcp_bun\n"
        "      owner: fleethub\n"
    )
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["listen"][0]["port"] == 8559  # stx-allow: STX-NL001


def test_listen_unix_socket_entry_path_in_status(tmp_path):
    # Arrange
    extra = (
        "  listen:\n"
        "    - proto: unix\n"
        "      path: /tmp/fleethub.sock\n"
        "      name: heartbeat\n"
        "      owner: fleethub\n"
    )
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["listen"][0]["path"] == "/tmp/fleethub.sock"


def test_hooks_configured_pre_start_count_in_status(tmp_path):
    # Arrange — v2 loader may auto-inject mkdir; assert at-least-2 user entries
    extra = "  hooks:\n    pre_start:\n      - echo a\n      - echo b\n"
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["hooks_configured"]["pre_start"] >= 2


def test_hooks_configured_on_compact_count_in_status(tmp_path):
    # Arrange
    extra = "  hooks:\n    on_compact:\n      - https://example.com/compact\n"
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["hooks_configured"]["on_compact"] == 1


def test_hooks_configured_unset_on_diff_is_zero(tmp_path):
    # Arrange — no on_diff declared
    extra = "  hooks:\n    pre_start:\n      - echo a\n"
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert result["hooks_configured"]["on_diff"] == 0


def test_status_does_not_leak_shell_command_bodies(tmp_path):
    # Arrange
    extra = "  hooks:\n    pre_start:\n      - echo SECRET_VALUE\n"
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert "SECRET_VALUE" not in json.dumps(result)


def test_status_does_not_leak_http_hook_urls(tmp_path):
    # Arrange
    extra = "  hooks:\n    on_compact:\n      - https://example.com/compact\n"
    # Act
    result = _real_status(tmp_path, extra)
    # Assert
    assert "example.com/compact" not in json.dumps(result)
