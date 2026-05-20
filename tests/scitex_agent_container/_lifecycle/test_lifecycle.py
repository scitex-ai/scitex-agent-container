"""Tests for ``scitex_agent_container._lifecycle.lifecycle`` — no mocks.

All collaborators are real:
  * ``AgentConfig`` is built either by the real loader (from a real
    on-disk YAML) or by direct construction of the production
    dataclass — no ``SimpleNamespace``/``MagicMock`` stand-ins.
  * The ``runtime_factory`` and ``handover_mod`` seams are exercised
    via hand-rolled fake classes whose surface matches the production
    contract (``is_running``/``start``/``stop``/``logs`` and
    ``ensure_instance_uuid``/``hydrate_from_hub``/...).
  * The ``Registry`` is the real on-disk file-based ``Registry`` rooted
    in ``tmp_path``.
  * ``sleep_fn`` / ``thread_factory`` / ``runner`` are real Python
    callables (no monkeypatching).

The shared ``home`` redirection used to use ``monkeypatch.setattr`` on
``Path.home``; we instead set ``HOME`` via a ``yield``-based env-var
fixture which is what ``Path.home()`` reads on POSIX.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig

# ---------------------------------------------------------------------------
# Fixtures — real env, real Registry, real YAML on disk
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path) -> Iterator[None]:
    # Arrange: redirect HOME so production code that uses Path.home()
    # lands inside tmp_path instead of the developer's real home.
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


def _write_spec(
    tmp_path: Path,
    *,
    name: str = "alpha",
    runtime: str = "apptainer",
    extra_spec: str = "",
) -> Path:
    """Write a real, validator-passing v3 YAML at ``<tmp>/<name>/spec.yaml``.

    Production ``load_config`` derives the agent name from the parent
    directory (dir-as-SSoT), so the spec must live at ``<name>/spec.yaml``.
    """
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        f"  runtime: {runtime}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  hooks:\n"
        "    pre_start: ['echo pre']\n"
        "    post_start: ['echo post']\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
        f"{extra_spec}"
    )
    spec = agent_dir / "spec.yaml"
    spec.write_text(body)
    return spec


# ---------------------------------------------------------------------------
# Real hand-rolled fakes (no unittest.mock)
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Real collaborator implementing the runtime surface lifecycle uses.

    Matches ``is_running``/``start``/``stop``/``logs``. Records calls so
    tests can assert on them. ``start_kwargs`` captures the most recent
    keyword arguments passed to :meth:`start`.
    """

    def __init__(
        self,
        *,
        running: bool = False,
        start_result: bool = True,
        logs_text: str = "log-content",
    ) -> None:
        self.running = running
        self.start_result = start_result
        self.logs_text = logs_text
        self.start_calls: list[AgentConfig] = []
        self.stop_calls: list[AgentConfig] = []
        self.start_kwargs: dict[str, Any] = {}
        self.stop_should_raise: Exception | None = None
        self.start_type_error: bool = False

    def is_running(self, config: AgentConfig) -> bool:
        return self.running

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        if self.start_type_error and kwargs.get("dry_run"):
            raise TypeError("got unexpected kw 'dry_run'")
        self.start_calls.append(config)
        self.start_kwargs = dict(kwargs)
        return self.start_result

    def stop(self, config: AgentConfig) -> None:
        if self.stop_should_raise is not None:
            raise self.stop_should_raise
        self.stop_calls.append(config)

    def logs(self, config: AgentConfig, lines: int) -> str:
        return self.logs_text


class FakeHandover:
    """Real collaborator implementing the handover module surface.

    Matches the four module-level callables lifecycle dispatches to:
    ``ensure_instance_uuid``, ``hydrate_from_hub``,
    ``push_pre_stop_snapshot``, ``start_failback_poller``.
    """

    def __init__(self) -> None:
        self.ensure_calls: list[AgentConfig] = []
        self.hydrate_calls: list[AgentConfig] = []
        self.pre_stop_calls: list[AgentConfig] = []
        self.failback_calls: list[AgentConfig] = []
        self.hydrate_raises: Exception | None = None
        self.failback_raises: Exception | None = None

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        self.ensure_calls.append(config)
        return "fake-uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        self.hydrate_calls.append(config)
        if self.hydrate_raises is not None:
            raise self.hydrate_raises
        return True

    def push_pre_stop_snapshot(
        self, config: AgentConfig, payload: dict | None = None
    ) -> bool:
        self.pre_stop_calls.append(config)
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        self.failback_calls.append(config)
        if self.failback_raises is not None:
            raise self.failback_raises


class FakeThread:
    """Real Thread-shaped collaborator.

    Records construction args + whether :meth:`start` was called. The
    target callable is NOT invoked — it's the production code's
    responsibility to pass a real ``threading.Thread`` for actual
    parallelism; tests substitute this hand-rolled fake.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True


class _FakeResult:
    """Real callable result object with the subprocess.run shape."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


def _no_sleep(_seconds: float) -> None:
    return None


# ---------------------------------------------------------------------------
# _get_runtime — purely declarative dispatch on config.runtime
# ---------------------------------------------------------------------------


def test_get_runtime_apptainer_returns_claude_session_runtime() -> None:
    # Arrange
    cfg = AgentConfig(name="x", runtime="apptainer")
    # Act
    rt = lc._get_runtime(cfg)
    # Assert
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_empty_runtime_treated_as_apptainer() -> None:
    # Arrange: explicit empty string runtime falls through to apptainer.
    cfg = AgentConfig(name="x", runtime="")
    # Act
    rt = lc._get_runtime(cfg)
    # Assert
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_unsupported_runtime_raises() -> None:
    # Arrange
    cfg = AgentConfig(name="x", runtime="docker-legacy")
    # Act
    call = lambda: lc._get_runtime(cfg)  # noqa: E731
    # Assert
    with pytest.raises(ValueError, match="Unsupported runtime"):
        call()


# ---------------------------------------------------------------------------
# _fallback_workdir — pure path string
# ---------------------------------------------------------------------------


def test_fallback_workdir_lands_under_sac_runtime_agents(tmp_path: Path) -> None:
    # Arrange (HOME already redirected to tmp_path by autouse fixture).
    # Act
    path = lc._fallback_workdir("alpha")
    # Assert
    assert path.endswith("/.scitex/agent-container/runtime/agents/alpha")


# ---------------------------------------------------------------------------
# _run_hooks — injected real runner, skips URLs + empty, logs failures
# ---------------------------------------------------------------------------


def test_run_hooks_executes_each_non_empty_non_url_hook() -> None:
    # Arrange
    calls: list[str] = []

    def runner(cmd: str, **_kwargs: Any) -> _FakeResult:
        calls.append(cmd)
        return _FakeResult()

    # Act
    lc._run_hooks(
        ["echo hi", "", "http://skip-me"], extra_env={"X": "1"}, runner=runner
    )
    # Assert
    assert calls == ["echo hi"]


def test_run_hooks_skips_https_url_entries() -> None:
    # Arrange
    calls: list[str] = []

    def runner(cmd: str, **_kwargs: Any) -> _FakeResult:
        calls.append(cmd)
        return _FakeResult()

    # Act
    lc._run_hooks(["https://hook.example/h"], runner=runner)
    # Assert
    assert calls == []


def test_run_hooks_warns_on_nonzero_returncode(capsys: pytest.CaptureFixture) -> None:
    # Arrange
    def runner(cmd: str, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(returncode=2, stderr="boom")

    # Act
    lc._run_hooks(["false"], runner=runner)
    # Assert
    err = capsys.readouterr().err
    assert "Hook failed" in err and "boom" in err


def test_run_hooks_real_subprocess_executes(tmp_path: Path) -> None:
    # Arrange: with the real ``subprocess.run`` default, an existing
    # command must complete without raising and produce a side-effect
    # we can observe on disk.
    sentinel = tmp_path / "marker"
    # Act
    lc._run_hooks([f"touch {sentinel}"])
    # Assert
    assert sentinel.exists()


# ---------------------------------------------------------------------------
# _fire_forget_hook — swallows exceptions from the underlying run_hook
# ---------------------------------------------------------------------------


def test_fire_forget_hook_swallows_run_hook_exceptions() -> None:
    # Arrange: route the module-level ``run_hook`` symbol used by
    # ``_fire_forget_hook`` to a real callable that raises. We restore
    # the original at the end so we don't leak state across tests.
    original = lc.run_hook

    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("hook crash")

    lc.run_hook = boom  # real callable, not Mock
    try:
        # Act: must not raise.
        lc._fire_forget_hook("alpha", "pre_start", ["echo hi"])
        # Assert: reaching this line means the exception was swallowed.
        assert True
    finally:
        lc.run_hook = original


# ---------------------------------------------------------------------------
# agent_start — happy paths, force, dry_run, hooks, health monitor
# ---------------------------------------------------------------------------


def test_agent_start_happy_path_returns_true(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=True)
    handover = FakeHandover()
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=handover,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert ok is True


def test_agent_start_happy_path_calls_runtime_start_once(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
    )
    # Assert
    assert len(runtime.start_calls) == 1


def test_agent_start_happy_path_registers_agent(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
    )
    # Assert
    assert registry.exists("alpha")


def test_agent_start_happy_path_invokes_handover(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    handover = FakeHandover()
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(start_result=True),
        handover_mod=handover,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert len(handover.ensure_calls) == 1 and len(handover.hydrate_calls) == 1


def test_agent_start_idempotent_when_already_running(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: registry knows the agent and runtime reports it running.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
    )
    # Assert: returns success and never calls start.
    assert ok is True and runtime.start_calls == []


def test_agent_start_force_restarts_when_already_running(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=True, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
    )
    # Assert: force triggered stop AND start on the same runtime instance.
    assert len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1


def test_agent_start_force_clears_stale_registry_entry(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: registered but runtime says not running → stale entry.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
    )
    # Assert
    assert ok is True and len(runtime.stop_calls) == 1


def test_agent_start_session_override_mutates_claude_session(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    captured: dict[str, AgentConfig] = {}

    def factory(c: AgentConfig) -> FakeRuntime:
        captured["cfg"] = c
        return FakeRuntime(start_result=True)

    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=factory,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        session_override="resume",
        resume_id_override="abc-123",
    )
    # Assert
    assert captured["cfg"].claude.session == "resume"


def test_agent_start_resume_id_override_mutates_resume_id(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    captured: dict[str, AgentConfig] = {}

    def factory(c: AgentConfig) -> FakeRuntime:
        captured["cfg"] = c
        return FakeRuntime(start_result=True)

    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=factory,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        session_override="resume",
        resume_id_override="abc-123",
    )
    # Assert
    assert captured["cfg"].claude.resume_id == "abc-123"


def test_agent_start_runtime_failure_raises_runtime_error(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=False)
    # Act
    call = lambda: lc.agent_start(  # noqa: E731
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
    )
    # Assert
    with pytest.raises(RuntimeError, match="Failed to start"):
        call()


def test_agent_start_dry_run_does_not_register(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        dry_run=True,
    )
    # Assert
    assert ok is True and not registry.exists("alpha")


def test_agent_start_dry_run_passes_dry_run_kwarg_to_runtime(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        dry_run=True,
    )
    # Assert
    assert runtime.start_kwargs.get("dry_run") is True


def test_agent_start_dry_run_typeerror_raises_helpful_runtime_error(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: a runtime whose ``start`` refuses ``dry_run`` (older runtime).
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(running=False)
    runtime.start_type_error = True
    # Act
    call = lambda: lc.agent_start(  # noqa: E731
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        dry_run=True,
    )
    # Assert
    with pytest.raises(RuntimeError, match="does not support --dry-run"):
        call()


def test_agent_start_hydrate_failure_does_not_block_start(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: hydrate_from_hub raises but agent_start must still succeed.
    spec = _write_spec(tmp_path)
    handover = FakeHandover()
    handover.hydrate_raises = RuntimeError("hub down")
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(start_result=True),
        handover_mod=handover,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert ok is True


def test_agent_start_starts_health_monitor_thread_when_enabled(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: health.enabled=true in the spec → production must spawn a thread.
    extra = "  health:\n    enabled: true\n    method: sdk-alive\n    interval: 0\n"
    spec = _write_spec(tmp_path, extra_spec=extra)
    created: list[FakeThread] = []

    def factory(*args: Any, **kwargs: Any) -> FakeThread:
        t = FakeThread(*args, **kwargs)
        created.append(t)
        return t

    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(start_result=True),
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        thread_factory=factory,
    )
    # Assert: the production code instantiated and started exactly one thread.
    assert len(created) == 1 and created[0].started is True


def test_agent_start_failback_poller_failure_is_swallowed(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    handover = FakeHandover()
    handover.failback_raises = RuntimeError("nope")
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(start_result=True),
        handover_mod=handover,
        sleep_fn=_no_sleep,
    )
    # Assert
    assert ok is True


def test_agent_start_cli_no_preflight_propagates_to_runtime(
    tmp_path: Path, registry: Registry
) -> None:
    """WI-6 removed ``RemoteSpec`` and the
    ``cfg.remote.no_preflight`` config-level override. Only the
    ``--no-preflight`` CLI flag now controls the runtime's
    preflight behaviour. This test exercises that single seam.
    """
    # Arrange
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(start_result=True)

    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        no_preflight=True,
    )
    # Assert
    assert runtime.start_kwargs.get("no_preflight") is True


# ---------------------------------------------------------------------------
# agent_stop
# ---------------------------------------------------------------------------


def test_agent_stop_unknown_agent_without_force_raises(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange (empty registry).
    # Act
    call = lambda: lc.agent_stop(  # noqa: E731
        "ghost",
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


def test_agent_stop_unknown_agent_with_force_returns_true(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange (empty registry).
    # Act
    ok = lc.agent_stop(
        "ghost",
        registry=registry,
        force=True,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
    )
    # Assert
    assert ok is True


def test_agent_stop_happy_path_returns_true(tmp_path: Path, registry: Registry) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime()
    # Act
    ok = lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
    )
    # Assert
    assert ok is True


def test_agent_stop_happy_path_calls_runtime_stop(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime()
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
    )
    # Assert
    assert len(runtime.stop_calls) == 1


def test_agent_stop_happy_path_removes_registry_entry(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    lc.agent_stop(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
    )
    # Assert
    assert not registry.exists("alpha")


def test_agent_stop_yaml_gone_with_force_succeeds(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: registry points at a YAML that no longer exists on disk.
    missing_spec = tmp_path / "alpha" / "spec.yaml"
    registry.add("alpha", str(missing_spec), "cld-alpha")
    # Act
    ok = lc.agent_stop(
        "alpha",
        registry=registry,
        force=True,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
    )
    # Assert
    assert ok is True and not registry.exists("alpha")


def test_agent_stop_yaml_gone_without_force_raises(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    missing_spec = tmp_path / "alpha" / "spec.yaml"
    registry.add("alpha", str(missing_spec), "cld-alpha")
    # Act
    call = lambda: lc.agent_stop(  # noqa: E731
        "alpha",
        registry=registry,
        force=False,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
    )
    # Assert
    with pytest.raises(FileNotFoundError):
        call()


def test_agent_stop_runtime_stop_failure_with_force_removes_entry(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime()
    runtime.stop_should_raise = RuntimeError("stop failed")
    # Act
    ok = lc.agent_stop(
        "alpha",
        registry=registry,
        force=True,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
    )
    # Assert
    assert ok is True and not registry.exists("alpha")


def test_agent_stop_runtime_stop_failure_without_force_raises(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime()
    runtime.stop_should_raise = RuntimeError("stop failed")
    # Act
    call = lambda: lc.agent_stop(  # noqa: E731
        "alpha",
        registry=registry,
        force=False,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
    )
    # Assert
    with pytest.raises(RuntimeError):
        call()


# ---------------------------------------------------------------------------
# agent_stop_all — injected real per-agent stop callable
# ---------------------------------------------------------------------------


def test_agent_stop_all_iterates_every_registry_entry(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    registry.add("beta", str(spec), "cld-beta")

    def stop_fn(name: str, registry: Registry, force: bool = False) -> bool:
        return True

    # Act
    results = lc.agent_stop_all(registry=registry, stop_fn=stop_fn)
    # Assert
    assert {r[0] for r in results} == {"alpha", "beta"}


def test_agent_stop_all_with_force_continues_through_errors(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    registry.add("beta", str(spec), "cld-beta")

    def stop_fn(name: str, registry: Registry, force: bool = False) -> bool:
        if name == "alpha":
            raise RuntimeError("first one fails")
        return True

    # Act
    results = lc.agent_stop_all(registry=registry, force=True, stop_fn=stop_fn)
    # Assert
    assert len(results) == 2 and results[0][1] is False


def test_agent_stop_all_without_force_aborts_on_first_failure(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    registry.add("beta", str(spec), "cld-beta")

    def stop_fn(name: str, registry: Registry, force: bool = False) -> bool:
        raise RuntimeError("nope")

    # Act
    results = lc.agent_stop_all(registry=registry, force=False, stop_fn=stop_fn)
    # Assert
    assert len(results) == 1 and results[0][1] is False


# ---------------------------------------------------------------------------
# agent_restart
# ---------------------------------------------------------------------------


def test_agent_restart_calls_runtime_stop_then_start(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(start_result=True)
    # Act
    ok = lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
    )
    # Assert
    assert ok is True and len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1


def test_agent_restart_unknown_raises(tmp_path: Path, registry: Registry) -> None:
    # Arrange (empty registry).
    # Act
    call = lambda: lc.agent_restart(  # noqa: E731
        "ghost",
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(),
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


# ---------------------------------------------------------------------------
# agent_status
# ---------------------------------------------------------------------------


def test_agent_status_unknown_raises(tmp_path: Path, registry: Registry) -> None:
    # Arrange (empty registry).
    # Act
    call = lambda: lc.agent_status(  # noqa: E731
        "ghost", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


def test_agent_status_running_reports_status_running(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(running=True)
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: runtime
    )
    # Assert
    assert result["status"] == "running"


def test_agent_status_includes_hooks_configured_counts(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime(running=True)
    )
    # Assert: spec writes one pre_start hook; production must echo a count >= 1.
    assert result["hooks_configured"]["pre_start"] >= 1


def test_agent_status_includes_empty_listen_and_extensions(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["listen"] == [] and result["extensions"] == {}


def test_agent_status_config_load_failure_degrades_to_stopped(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: register a path to a non-existent YAML so load_config raises.
    registry.add("alpha", str(tmp_path / "alpha" / "spec.yaml"), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["status"] == "stopped"


def test_agent_status_config_load_failure_reports_unknown_model_and_runtime(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    registry.add("alpha", str(tmp_path / "alpha" / "spec.yaml"), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["model"] == "unknown" and result["runtime"] == "unknown"


def test_agent_status_omits_remote_host_after_wi6_deletion(
    tmp_path: Path, registry: Registry
) -> None:
    """WI-6 (handoff §6, 2026-05-20) deleted ``RemoteSpec`` and the
    ``cfg.remote`` attribute, so ``agent_status`` no longer emits a
    ``remote`` key. v3 host pinning lives in ``spec.host`` and is
    surfaced via ``sac host`` / state.db ``instances.host`` instead.
    """
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime_factory = lambda _c: FakeRuntime(running=True)  # noqa: E731
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=runtime_factory
    )
    # Assert
    assert "remote" not in result


# ---------------------------------------------------------------------------
# agent_logs
# ---------------------------------------------------------------------------


def test_agent_logs_unknown_raises(tmp_path: Path, registry: Registry) -> None:
    # Arrange (empty registry).
    # Act
    call = lambda: lc.agent_logs(  # noqa: E731
        "ghost", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


def test_agent_logs_returns_runtime_logs_text(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = FakeRuntime(logs_text="log-content")
    # Act
    out = lc.agent_logs(
        "alpha",
        lines=10,
        registry=registry,
        runtime_factory=lambda _c: runtime,
    )
    # Assert
    assert out == "log-content"
