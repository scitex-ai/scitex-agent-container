"""Tests for hooks.run_hook and lifecycle wiring (todo#286 Phase 4).

Covers:
  - shell + http dispatch paths, error swallowing, fire-and-forget
  - lifecycle pre_start / post_start invoke run_hook
  - context_manager on_compact fires run_hook
  - snapshot on_diff fires run_hook
  - status --json exposes extensions, listen, hooks_configured counts
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container import hooks

# ---------------------------------------------------------------------------
# run_hook — unit tests (patch the pool to run synchronously)
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_pool(monkeypatch):
    """Make run_hook inline so we can assert side-effects deterministically."""

    class _InlinePool:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

            class _F:
                def result(self_inner, timeout=None):
                    return None

            return _F()

    monkeypatch.setattr(hooks, "_POOL", _InlinePool())
    return _InlinePool


def test_run_hook_shell_success(sync_pool, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env", {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks.run_hook("agent-x", "pre_start", ["echo hello world"], context={"k": "v"})
    assert seen["argv"] == ["echo", "hello", "world"]
    assert seen["env"]["SAC_NAME"] == "agent-x"
    assert seen["env"]["SCITEX_HOOK"] == "pre_start"
    assert seen["env"]["SCITEX_HOOK_CTX_K"] == "v"


def test_run_hook_shell_failure_swallowed(sync_pool, monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, "", "boom")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    # Must not raise.
    hooks.run_hook("agent-x", "pre_start", ["/bin/false"])


def test_run_hook_http_success(sync_pool, monkeypatch):
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(hooks.urlrequest, "urlopen", fake_urlopen)
    hooks.run_hook(
        "agent-y",
        "on_compact",
        ["https://example.com/hook"],
        context={"percent": 90.0},
    )
    assert captured["url"] == "https://example.com/hook"
    assert captured["method"] == "POST"
    payload = json.loads(captured["body"].decode())
    assert payload["agent"] == "agent-y"
    assert payload["hook"] == "on_compact"
    assert payload["context"] == {"percent": 90.0}
    assert captured["timeout"] == hooks._HTTP_TIMEOUT_S


def test_run_hook_http_failure_swallowed(sync_pool, monkeypatch):
    from urllib import error as urlerror

    def fake_urlopen(req, timeout=None):
        raise urlerror.URLError("nope")

    monkeypatch.setattr(hooks.urlrequest, "urlopen", fake_urlopen)
    hooks.run_hook("agent-y", "on_compact", ["http://bad.invalid/"])
    # reaching here = swallowed


def test_run_hook_fire_and_forget(monkeypatch):
    submitted = []

    class _FakePool:
        def submit(self, fn, *args, **kwargs):
            submitted.append((fn, args, kwargs))

    monkeypatch.setattr(hooks, "_POOL", _FakePool())
    hooks.run_hook("a", "pre_start", ["echo 1", "echo 2"])
    assert len(submitted) == 2
    # run_hook must return without blocking (no exception, no wait)


def test_run_hook_empty_commands_noop(monkeypatch):
    called = []
    monkeypatch.setattr(
        hooks, "_POOL", SimpleNamespace(submit=lambda *a, **k: called.append(a))
    )
    hooks.run_hook("a", "pre_start", None)
    hooks.run_hook("a", "pre_start", [])
    assert called == []


# ---------------------------------------------------------------------------
# Lifecycle wiring
# ---------------------------------------------------------------------------


def test_agent_start_invokes_pre_and_post(monkeypatch, tmp_path):
    from scitex_agent_container._lifecycle import lifecycle

    calls: list[tuple[str, list[str]]] = []

    def fake_run_hook(agent, hook_name, commands, context=None):
        calls.append((hook_name, list(commands or [])))

    monkeypatch.setattr(lifecycle, "run_hook", fake_run_hook)

    # Build a fake config + runtime + registry.
    cfg = SimpleNamespace(
        name="a1",
        screen_name="a1",
        hooks={"pre_start": ["echo pre"], "post_start": ["echo post"]},
        context_management=SimpleNamespace(enabled=False),
        health=SimpleNamespace(enabled=False),
        remote=SimpleNamespace(no_preflight=True, is_remote=False, host=""),
    )

    class _Runtime:
        def is_running(self, c):
            return False

        def start(self, c, no_preflight=False, force=False, **_kw):
            return True

    class _Registry:
        def __init__(self):
            self.added = False

        def exists(self, name):
            return False

        def add(self, **kw):
            self.added = True

    config_file = tmp_path / "a1.yaml"
    config_file.write_text("apiVersion: v1\n")

    monkeypatch.setattr(lifecycle, "resolve_config", lambda p: str(config_file))
    monkeypatch.setattr(lifecycle, "load_config", lambda p: cfg)
    monkeypatch.setattr(lifecycle, "_get_runtime", lambda c: _Runtime())
    # Silence the legacy shell path — it shells out to /bin/sh otherwise.
    monkeypatch.setattr(lifecycle, "_run_hooks", lambda *a, **k: None)

    assert lifecycle.agent_start(str(config_file), registry=_Registry()) is True

    hook_names = [c[0] for c in calls]
    assert "pre_start" in hook_names
    assert "post_start" in hook_names


def test_snapshot_on_diff_invoked_when_has_diff(monkeypatch, tmp_path):
    from scitex_agent_container._state import snapshot

    monkeypatch.setenv("SAC_CACHE_DIR", str(tmp_path))

    seen = []

    def fake_run_hook(agent, hook_name, commands, context=None):
        seen.append((hook_name, list(commands or []), dict(context or {})))

    monkeypatch.setattr(hooks, "run_hook", fake_run_hook)

    # Force gather_snapshot to return monotonically changing payloads.
    counter = {"n": 0}

    def fake_gather(agent, *, session=None):
        counter["n"] += 1
        return {
            "agent": agent,
            "timestamp": f"t{counter['n']}",
            "tmux_count": counter["n"],
        }

    monkeypatch.setattr(snapshot, "gather_snapshot", fake_gather)

    agent_cfg = SimpleNamespace(hooks={"on_diff": ["echo diffed"]})

    # First tick: no prev → no diff fields (empty diff).
    snapshot.snapshot_tick("a3", agent_config=agent_cfg)
    # Second tick: tmux_count changes → has_diff.
    snapshot.snapshot_tick("a3", agent_config=agent_cfg)

    diff_calls = [s for s in seen if s[0] == "on_diff"]
    assert diff_calls, f"expected on_diff call, got {seen}"
    assert diff_calls[-1][1] == ["echo diffed"]
    assert "diff_fields" in diff_calls[-1][2]


# ---------------------------------------------------------------------------
# Status --json enrichment
# ---------------------------------------------------------------------------


def _write_v2_config(tmp_path: Path, extra: str = "") -> Path:
    """v3: dir-as-SSoT — YAML lives at <name>/<name>.yaml, no metadata.name."""
    d = tmp_path / "statustest"
    d.mkdir(exist_ok=True)
    p = d / "statustest.yaml"
    p.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  model: sonnet\n" + extra
    )
    return p


def _load(tmp_path: Path, extra: str = ""):
    from scitex_agent_container.config import load_config

    return load_config(_write_v2_config(tmp_path, extra))


def _status_for(monkeypatch, tmp_path, extra):
    from scitex_agent_container._lifecycle import lifecycle

    cfg = _load(tmp_path, extra)

    class _Registry:
        def get(self, name):
            return {
                "config": cfg.config_path,
                "screen": cfg.screen_name,
                "started_at": "2026-04-12T00:00:00Z",
            }

    class _Runtime:
        def is_running(self, c):
            return False

    monkeypatch.setattr(lifecycle, "_get_runtime", lambda c: _Runtime())
    return lifecycle.agent_status("statustest", registry=_Registry())


def test_extensions_passthrough_in_status(monkeypatch, tmp_path):
    extra = "  extensions:\n    orochi:\n      foo: bar\n      nested:\n        a: 1\n"
    result = _status_for(monkeypatch, tmp_path, extra)
    assert result["extensions"] == {"orochi": {"foo": "bar", "nested": {"a": 1}}}


def test_listen_declarations_in_status(monkeypatch, tmp_path):
    extra = (
        "  listen:\n"
        "    - port: 8559\n"
        "      proto: tcp\n"
        "      name: mcp_bun\n"
        "      owner: orochi\n"
        "    - proto: unix\n"
        "      path: /tmp/orochi.sock\n"
        "      name: heartbeat\n"
        "      owner: orochi\n"
    )
    result = _status_for(monkeypatch, tmp_path, extra)
    listen = result["listen"]
    assert len(listen) == 2
    assert listen[0]["port"] == 8559
    assert listen[0]["proto"] == "tcp"
    assert listen[0]["name"] == "mcp_bun"
    assert listen[1]["proto"] == "unix"
    assert listen[1]["path"] == "/tmp/orochi.sock"


def test_hooks_configured_counts_in_status(monkeypatch, tmp_path):
    extra = (
        "  hooks:\n"
        "    pre_start:\n"
        "      - echo a\n"
        "      - echo b\n"
        "    on_compact:\n"
        "      - https://example.com/compact\n"
    )
    result = _status_for(monkeypatch, tmp_path, extra)
    counts = result["hooks_configured"]
    # pre_start has auto-injected mkdir (v2) + two user entries == 3
    assert counts["pre_start"] >= 2
    assert counts["on_compact"] == 1
    assert counts["on_diff"] == 0
    # Command bodies never leak.
    assert "echo a" not in json.dumps(result)
    assert "https://example.com/compact" not in json.dumps(result)
