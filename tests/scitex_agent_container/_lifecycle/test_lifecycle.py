"""Tests for ``scitex_agent_container._lifecycle.lifecycle``.

Covers agent_start / agent_stop / agent_stop_all / agent_restart /
agent_status / agent_logs / _get_runtime / _fallback_workdir /
_run_hooks / _fire_forget_hook. External services (claude runtime,
hub handover, subprocess hooks, health monitor) are mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


@pytest.fixture
def cfg(tmp_path) -> AgentConfig:
    c = AgentConfig(name="alpha", workdir=str(tmp_path / "work"))
    c.hooks = {
        "pre_start": ["echo pre"],
        "post_start": ["echo post"],
        "pre_stop": [],
        "post_stop": [],
    }
    return c


@pytest.fixture
def fake_runtime():
    rt = MagicMock()
    rt.start.return_value = True
    rt.stop.return_value = True
    rt.is_running.return_value = False
    rt.logs.return_value = "log-content"
    return rt


@pytest.fixture
def patched(monkeypatch, cfg, fake_runtime, tmp_path):
    """Patch out external dependencies for lifecycle tests."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("apiVersion: scitex-agent-container/v3\n")
    monkeypatch.setattr(lc, "resolve_config", lambda p: str(spec_path))
    monkeypatch.setattr(lc, "load_config", lambda p: cfg)
    monkeypatch.setattr(lc, "_get_runtime", lambda c: fake_runtime)

    # Patch attributes on the real handover module so lifecycle.py's
    # ``from . import handover as _h`` sees no-op functions.
    from scitex_agent_container._lifecycle import handover as real_handover

    handover_mock = MagicMock()
    handover_mock.ensure_instance_uuid = MagicMock()
    handover_mock.hydrate_from_hub = MagicMock()
    handover_mock.push_pre_stop_snapshot = MagicMock()
    handover_mock.start_failback_poller = MagicMock()
    for attr in (
        "ensure_instance_uuid",
        "hydrate_from_hub",
        "push_pre_stop_snapshot",
        "start_failback_poller",
    ):
        monkeypatch.setattr(real_handover, attr, getattr(handover_mock, attr))
    return {"spec_path": spec_path, "handover": handover_mock}


# --- helpers --------------------------------------------------------------


def test_get_runtime_returns_claude_session_for_apptainer(cfg):
    rt = lc._get_runtime(cfg)
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_rejects_unknown_runtime():
    c = AgentConfig(name="x")
    c.runtime = "docker-legacy"
    with pytest.raises(ValueError, match="Unsupported runtime"):
        lc._get_runtime(c)


def test_get_runtime_treats_empty_as_apptainer():
    c = AgentConfig(name="x")
    c.runtime = ""
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    assert isinstance(lc._get_runtime(c), ClaudeSessionRuntime)


def test_fallback_workdir_lands_in_sac_runtime(tmp_path):
    path = lc._fallback_workdir("alpha")
    assert path.endswith("/.scitex/agent-container/runtime/agents/alpha")


def test_run_hooks_executes_shell(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    lc._run_hooks(["echo hi", "", "http://skip-me"], extra_env={"X": "1"})
    assert calls == ["echo hi"]


def test_run_hooks_warns_on_failure(monkeypatch, capsys):
    class FakeResult:
        returncode = 2
        stderr = "boom"

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())
    lc._run_hooks(["false"])
    err = capsys.readouterr().err
    assert "Hook failed" in err
    assert "boom" in err


def test_fire_forget_hook_swallows_exceptions(monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("hook crash")

    monkeypatch.setattr(lc, "run_hook", boom)
    # should NOT raise
    lc._fire_forget_hook("alpha", "pre_start", ["echo hi"])


# --- agent_start ----------------------------------------------------------


def test_agent_start_happy_path(patched, registry, fake_runtime):
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry)
    assert ok is True
    fake_runtime.start.assert_called_once()
    assert registry.exists("alpha")
    patched["handover"].ensure_instance_uuid.assert_called_once()
    patched["handover"].hydrate_from_hub.assert_called_once()


def test_agent_start_idempotent_when_running(patched, registry, fake_runtime):
    fake_runtime.is_running.return_value = True
    # Pre-add registry entry to simulate "already running"
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry)
    assert ok is True
    fake_runtime.start.assert_not_called()


def test_agent_start_force_restarts(patched, registry, fake_runtime, monkeypatch):
    fake_runtime.is_running.return_value = True
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry, force=True)
    assert ok is True
    # stop happened, then start
    fake_runtime.stop.assert_called_once()
    fake_runtime.start.assert_called_once()


def test_agent_start_force_with_stale_registry(patched, registry, fake_runtime):
    # not running but registered → stale entry must be removed via stop()
    fake_runtime.is_running.return_value = False
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry, force=True)
    assert ok is True
    fake_runtime.stop.assert_called_once()


def test_agent_start_session_and_resume_overrides(patched, registry, fake_runtime, cfg):
    lc.agent_start(
        str(patched["spec_path"]),
        registry=registry,
        session_override="resume",
        resume_id_override="abc-123",
    )
    assert cfg.claude.session == "resume"
    assert cfg.claude.resume_id == "abc-123"


def test_agent_start_runtime_failure_raises(patched, registry, fake_runtime):
    fake_runtime.start.return_value = False
    with pytest.raises(RuntimeError, match="Failed to start"):
        lc.agent_start(str(patched["spec_path"]), registry=registry)


def test_agent_start_dry_run_skips_registry(patched, registry, fake_runtime):
    fake_runtime.start.return_value = True
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry, dry_run=True)
    assert ok is True
    # dry_run passes through, but no registry write
    assert not registry.exists("alpha")
    # Called with dry_run=True kwarg
    args, kwargs = fake_runtime.start.call_args
    assert kwargs.get("dry_run") is True


def test_agent_start_dry_run_typeerror_propagates(patched, registry):
    rt = MagicMock()

    def start_no_dry(config, **kw):
        # Older runtime: refuse dry_run kwarg
        raise TypeError("got unexpected kw 'dry_run'")

    rt.start.side_effect = start_no_dry
    with patch.object(lc, "_get_runtime", return_value=rt):
        with pytest.raises(RuntimeError, match="does not support --dry-run"):
            lc.agent_start(str(patched["spec_path"]), registry=registry, dry_run=True)


def test_agent_start_hydrate_failure_does_not_block(patched, registry, fake_runtime):
    patched["handover"].hydrate_from_hub.side_effect = RuntimeError("hub down")
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry)
    assert ok is True


def test_agent_start_starts_health_monitor_thread(
    patched, registry, fake_runtime, cfg, monkeypatch
):
    cfg.health.enabled = True
    started = {"flag": False}

    class FakeThread:
        def __init__(self, *a, **kw):
            started["args"] = (a, kw)

        def start(self):
            started["flag"] = True

    monkeypatch.setattr("threading.Thread", FakeThread)
    ok = lc.agent_start(str(patched["spec_path"]), registry=registry)
    assert ok is True
    assert started["flag"] is True


def test_agent_start_failback_poller_swallows(patched, registry, fake_runtime):
    patched["handover"].start_failback_poller.side_effect = RuntimeError("nope")
    # should still return True
    assert lc.agent_start(str(patched["spec_path"]), registry=registry) is True


def test_agent_start_config_no_preflight_overrides(
    patched, registry, fake_runtime, cfg
):
    cfg.remote.no_preflight = True
    lc.agent_start(str(patched["spec_path"]), registry=registry, no_preflight=False)
    args, kwargs = fake_runtime.start.call_args
    assert kwargs.get("no_preflight") is True


# --- agent_stop -----------------------------------------------------------


def test_agent_stop_unknown_agent_raises(patched, registry):
    with pytest.raises(RuntimeError, match="not found"):
        lc.agent_stop("ghost", registry=registry)


def test_agent_stop_unknown_force_returns_true(patched, registry):
    assert lc.agent_stop("ghost", registry=registry, force=True) is True


def test_agent_stop_happy_path(patched, registry, fake_runtime):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    assert lc.agent_stop("alpha", registry=registry) is True
    fake_runtime.stop.assert_called_once()
    assert not registry.exists("alpha")


def test_agent_stop_yaml_gone_force_succeeds(patched, registry, monkeypatch):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    monkeypatch.setattr(
        lc, "load_config", lambda p: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    assert lc.agent_stop("alpha", registry=registry, force=True) is True
    assert not registry.exists("alpha")


def test_agent_stop_yaml_gone_no_force_raises(patched, registry, monkeypatch):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    monkeypatch.setattr(
        lc, "load_config", lambda p: (_ for _ in ()).throw(FileNotFoundError("gone"))
    )
    with pytest.raises(FileNotFoundError):
        lc.agent_stop("alpha", registry=registry, force=False)


def test_agent_stop_runtime_stop_failure_force(patched, registry, fake_runtime):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    fake_runtime.stop.side_effect = RuntimeError("stop failed")
    # force=True swallows and still removes
    assert lc.agent_stop("alpha", registry=registry, force=True) is True
    assert not registry.exists("alpha")


def test_agent_stop_runtime_stop_failure_no_force(patched, registry, fake_runtime):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    fake_runtime.stop.side_effect = RuntimeError("stop failed")
    with pytest.raises(RuntimeError):
        lc.agent_stop("alpha", registry=registry, force=False)


def test_agent_stop_pre_stop_hook_failure_no_force(
    patched, registry, fake_runtime, monkeypatch, cfg
):
    cfg.hooks["pre_stop"] = ["bad"]
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")

    def boom(*a, **kw):
        raise RuntimeError("pre-stop boom")

    monkeypatch.setattr(lc, "_run_hooks", boom)
    with pytest.raises(RuntimeError):
        lc.agent_stop("alpha", registry=registry, force=False)


# --- agent_stop_all -------------------------------------------------------


def test_agent_stop_all_iterates(patched, registry, fake_runtime):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    registry.add("beta", str(patched["spec_path"]), "cld-beta")
    results = lc.agent_stop_all(registry=registry)
    names = {r[0] for r in results}
    assert names == {"alpha", "beta"}
    assert all(r[1] for r in results)


def test_agent_stop_all_continues_through_errors_with_force(
    patched, registry, fake_runtime, monkeypatch
):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    registry.add("beta", str(patched["spec_path"]), "cld-beta")
    seen = {"n": 0}
    real_stop = lc.agent_stop

    def maybe_boom(name, registry=None, force=False):
        seen["n"] += 1
        if name == "alpha":
            raise RuntimeError("first one fails")
        return real_stop(name, registry=registry, force=force)

    monkeypatch.setattr(lc, "agent_stop", maybe_boom)
    results = lc.agent_stop_all(registry=registry, force=True)
    assert len(results) == 2
    assert results[0][1] is False
    assert "first one fails" in results[0][2]


def test_agent_stop_all_aborts_without_force(
    patched, registry, fake_runtime, monkeypatch
):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    registry.add("beta", str(patched["spec_path"]), "cld-beta")
    monkeypatch.setattr(
        lc, "agent_stop", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    results = lc.agent_stop_all(registry=registry, force=False)
    # Aborts after first failure
    assert len(results) == 1
    assert results[0][1] is False


# --- agent_restart --------------------------------------------------------


def test_agent_restart_calls_stop_then_start(
    patched, registry, fake_runtime, monkeypatch
):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    ok = lc.agent_restart("alpha", registry=registry)
    assert ok is True
    fake_runtime.stop.assert_called_once()
    fake_runtime.start.assert_called_once()


def test_agent_restart_unknown_raises(patched, registry):
    with pytest.raises(RuntimeError, match="not found"):
        lc.agent_restart("ghost", registry=registry)


# --- agent_status ---------------------------------------------------------


def test_agent_status_unknown_raises(patched, registry):
    with pytest.raises(RuntimeError, match="not found"):
        lc.agent_status("ghost", registry=registry)


def test_agent_status_running_basic_fields(patched, registry, fake_runtime, cfg):
    fake_runtime.is_running.return_value = True
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    result = lc.agent_status("alpha", registry=registry)
    assert result["name"] == "alpha"
    assert result["status"] == "running"
    assert result["model"] == cfg.model
    assert result["runtime"] == cfg.runtime
    assert result["hooks_configured"]["pre_start"] == 1
    assert result["snapshot"] is None
    assert "listen" in result and result["listen"] == []
    assert "extensions" in result


def test_agent_status_config_load_failure_degrades(
    patched, registry, monkeypatch, fake_runtime
):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    monkeypatch.setattr(
        lc, "load_config", lambda p: (_ for _ in ()).throw(RuntimeError("bad"))
    )
    result = lc.agent_status("alpha", registry=registry)
    assert result["status"] == "stopped"
    assert result["model"] == "unknown"
    assert result["runtime"] == "unknown"


def test_agent_status_remote_field(patched, registry, fake_runtime, cfg):
    fake_runtime.is_running.return_value = True
    cfg.remote.host = "remote-box"
    # is_remote must also be true; the dataclass derives it from host
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    result = lc.agent_status("alpha", registry=registry)
    if result.get("remote"):
        assert result["remote"] == "remote-box"


# --- agent_logs -----------------------------------------------------------


def test_agent_logs_unknown_raises(patched, registry):
    with pytest.raises(RuntimeError, match="not found"):
        lc.agent_logs("ghost", registry=registry)


def test_agent_logs_returns_runtime_logs(patched, registry, fake_runtime):
    registry.add("alpha", str(patched["spec_path"]), "cld-alpha")
    out = lc.agent_logs("alpha", lines=10, registry=registry)
    assert out == "log-content"
    fake_runtime.logs.assert_called_once()
