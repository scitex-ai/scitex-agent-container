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

import importlib
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._start_outcome import (
    KIND_ALREADY_RUNNING,
    outcome_kind,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig, load_config

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
    restart_block: str = "  restart:\n    policy: on-failure\n    max_retries: 3\n",
) -> Path:
    """Write a real, validator-passing v3 YAML at ``<tmp>/<name>/spec.yaml``.

    Production ``load_config`` derives the agent name from the parent
    directory (dir-as-SSoT), so the spec must live at ``<name>/spec.yaml``.
    """
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Omit the default apptainer block when a test supplies its own via
    # extra_spec — YAML can't carry two `apptainer:` keys (the second would
    # silently win and drop the required image/binds).
    apptainer_default = (
        ""
        if "apptainer:" in extra_spec
        else "  apptainer:\n    image: /x.sif\n    binds: []\n"
    )
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        f"  runtime: {runtime}\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        f"{apptainer_default}"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 60\n"
        f"{restart_block}"
        "  hooks:\n"
        "    pre_start: ['echo pre']\n"
        "    post_start: ['echo post']\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
        f"{extra_spec}"
    )
    # Red-start ruling 2026-07-21: merge the validator's paste defaults
    # beneath the composed body (body wins) so every field is explicit.
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    spec = agent_dir / "spec.yaml"
    spec.write_text(explicitize_yaml(body))
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
    # Arrange — legacy ``"apptainer"`` (the pre-2026-06-13 container-engine
    # selector) is back-compat-mapped to the headless SDK runner.
    cfg = AgentConfig(name="x", runtime="apptainer")
    # Act
    rt = lc._get_runtime(cfg)
    # Assert
    from scitex_agent_container.runtimes.claude_session import ClaudeSessionRuntime

    assert isinstance(rt, ClaudeSessionRuntime)


def test_get_runtime_empty_runtime_defaults_to_tui() -> None:
    # Arrange — operator directive 2026-06-15: post the SDK-pool cutoff, an
    # empty / unset ``spec.runtime`` selects the interactive in-apptainer
    # TUI runtime (the new default). Previously empty → ClaudeSessionRuntime;
    # the contract flipped when ``spec.runtime`` was repurposed from
    # container-engine selector to launch-mode selector (directive 12870 +
    # lead a2a ``b58dd5d3``). Pin the new default here so a future
    # accidental flip-back is caught.
    cfg = AgentConfig(name="x", runtime="")
    # Act
    rt = lc._get_runtime(cfg)
    # Assert
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    assert isinstance(rt, TuiSessionRuntime)


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


def test_run_hooks_warns_on_nonzero_returncode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    def runner(cmd: str, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(returncode=2, stderr="boom")

    # Act
    with caplog.at_level("WARNING"):
        lc._run_hooks(["false"], runner=runner)
    # Assert — the failure is logged at WARNING level with cmd + stderr.
    assert "Hook failed" in caplog.text and "boom" in caplog.text


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
    # ``_fire_forget_hook`` to a real callable that raises. ``_fire_forget_hook``
    # lives in ``_hook_runner`` (split out of the former monolith), and
    # resolves ``run_hook`` from that module's namespace, so patch there.
    # We restore the original at the end so we don't leak state across tests.
    from scitex_agent_container._lifecycle import _hook_runner

    original = _hook_runner.run_hook

    def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("hook crash")

    _hook_runner.run_hook = boom  # real callable, not Mock
    try:
        # Act: must not raise.
        lc._fire_forget_hook("alpha", "pre_start", ["echo hi"])
        # Assert: reaching this line means the exception was swallowed.
        assert True
    finally:
        _hook_runner.run_hook = original


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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
    )
    # Assert
    assert len(handover.ensure_calls) == 1 and len(handover.hydrate_calls) == 1


def test_agent_start_idempotent_when_already_running(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange: registry knows the agent, runtime reports it running,
    # AND the real-liveness verifier confirms an active instance row.
    # Without the verifier signal a registry+is_running false positive
    # would silently no-op a real start — see _verify_real_liveness.
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
        liveness_verifier=lambda _cfg, _rt: True,
        thread_factory=FakeThread,
    )
    # Assert: returns success, says WHY, and never calls start.
    #
    # `bool(ok)`, not `ok is True`: the no-op branch now returns the tagged
    # `NOOP_ALREADY_RUNNING` (an int subclass) rather than the `True`
    # SINGLETON, so identity no longer holds while truthiness — the actual
    # contract this test names — does. This is STRICTER than the old
    # assertion, not looser: it additionally pins WHICH branch produced the
    # success, a distinction the bare `True` made impossible and whose
    # absence let a restart report success over an agent that never cycled
    # (incident 2026-07-12). See :mod:`._lifecycle._start_outcome`.
    assert (
        bool(ok) is True
        and outcome_kind(ok) == KIND_ALREADY_RUNNING
        and runtime.start_calls == []
    )


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
        liveness_verifier=lambda _cfg, _rt: True,
        thread_factory=FakeThread,
    )
    # Assert: force triggered stop AND start on the same runtime instance.
    assert len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1


# ---------------------------------------------------------------------------
# Bug 1 (real-liveness): the already-running no-op MUST require three
# independent signals (registry on-disk entry + runtime PID liveness +
# active instances row). Pre-fix code trusted only the first two, so a
# stale registry file + reused PID looked identical to a real running
# agent and silently swallowed the start request as a no-op (exit 0).
# ---------------------------------------------------------------------------


def test_agent_start_launches_when_liveness_verifier_returns_false(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — registry says exists, runtime PID-check says running,
    # but the instances oracle reports NO active row (= stale state).
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
        liveness_verifier=lambda _cfg, _rt: False,
        thread_factory=FakeThread,
    )
    # Assert — fell through to a real launch instead of the silent no-op.
    assert len(runtime.start_calls) == 1


def test_verify_real_liveness_default_returns_true_for_recorded_instance(
    tmp_path: Path, isolated_state_db: Path
) -> None:
    # Arrange — write a real instances row and call the default verifier
    # against it.
    from scitex_agent_container._lifecycle._start import _verify_real_liveness
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="alpha", host="h", a2a_port=19111)
    cfg = AgentConfig(name="alpha")
    # Act
    live = _verify_real_liveness(cfg, runtime=None)
    # Assert
    assert live is True


def test_verify_real_liveness_default_returns_false_when_no_row(
    tmp_path: Path, isolated_state_db: Path
) -> None:
    # Arrange — fresh isolated state.db with no rows.
    from scitex_agent_container._lifecycle._start import _verify_real_liveness

    cfg = AgentConfig(name="alpha")
    # Act
    live = _verify_real_liveness(cfg, runtime=None)
    # Assert
    assert live is False


def test_verify_real_liveness_swallows_oracle_errors_as_not_live(
    tmp_path: Path,
) -> None:
    # Arrange — a broken oracle (raises) must degrade to "not live" so
    # the caller falls through to a real start instead of crashing.
    from scitex_agent_container._lifecycle._start import _verify_real_liveness

    def _broken():
        raise RuntimeError("state.db is wedged")

    cfg = AgentConfig(name="alpha")
    # Act
    live = _verify_real_liveness(cfg, runtime=None, instances_oracle=_broken)
    # Assert
    assert live is False


def test_verify_real_liveness_ignores_rows_for_other_agents(
    tmp_path: Path,
) -> None:
    # Arrange — oracle returns active rows for OTHER agents only; the
    # check is name-scoped so a busy cluster on other agents must not
    # be mistaken for liveness of the agent in question.
    from scitex_agent_container._lifecycle._start import _verify_real_liveness

    cfg = AgentConfig(name="alpha")
    # Act
    live = _verify_real_liveness(
        cfg,
        runtime=None,
        instances_oracle=lambda: [{"name": "beta"}, {"name": "gamma"}],
    )
    # Assert
    assert live is False


@pytest.fixture
def isolated_state_db(tmp_path: Path) -> Iterator[Path]:
    """Per-test on-disk state.db, exported via env (explicit save/restore).

    ``state_db`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB`` at import into a
    module-level ``DEFAULT_DB_PATH``; reload it after setting the env so the
    ``instances`` / ``a2a_ports`` writes land in the temp DB, not the
    developer's real ``~/.scitex`` tree. Mirrors the ``_instances`` test.
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _force_restart_running_agent(
    tmp_path: Path, registry: Registry, name: str = "alpha"
) -> None:
    """Drive a ``--force`` restart of a registered, running agent whose
    spec uses ``a2a.port: auto`` (the AgentConfig default), so resolution
    claims a real allocator port. Shared Act for the regression tests.

    Passes ``liveness_verifier=True`` so the three-signal
    already-running check fires even in the empty-state.db isolation
    fixture (real production would have the instances row recorded).
    """
    spec = _write_spec(tmp_path, name=name)
    registry.add(name, str(spec), f"cld-{name}")
    runtime = FakeRuntime(running=True, start_result=True)
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=FakeHandover(),
        sleep_fn=_no_sleep,
        force=True,
        liveness_verifier=lambda _cfg, _rt: True,
        thread_factory=FakeThread,
    )


def test_agent_start_force_restart_records_single_active_instance_row(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    # Act
    _force_restart_running_agent(tmp_path, registry)
    # Assert: exactly one active instances row survives the restart.
    rows = [r for r in list_active_instances() if r["name"] == "alpha"]
    assert len(rows) == 1


def test_agent_start_force_restart_records_non_none_a2a_port(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    """Regression: before the fix, the ``--force`` ``agent_stop`` released
    the port claim that the line-249 resolve had inserted, so
    ``record_local_instance`` read an empty ``a2a_ports`` table and wrote
    ``a2a_port=None`` — breaking ``/v1/turn`` routing even though the
    sidecar bound. The post-force-stop re-resolve keeps it non-None."""
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    # Act
    _force_restart_running_agent(tmp_path, registry)
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "alpha"][0]
    assert row["a2a_port"] is not None


def test_agent_start_force_restart_instances_port_matches_claim(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    """After a force restart the ``instances`` row a2a_port must equal the
    live ``a2a_ports`` claim — the two tables stay consistent so ``sac
    listen`` / ``/v1/turn`` agree on the port."""
    # Arrange
    from scitex_agent_container._state.port_allocator import get_port
    from scitex_agent_container._state.state_db import list_active_instances

    # Act
    _force_restart_running_agent(tmp_path, registry)
    # Assert
    row = [r for r in list_active_instances() if r["name"] == "alpha"][0]
    assert row["a2a_port"] == get_port("alpha")


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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
    )
    # Assert
    assert captured["cfg"].claude.session == "resume"


def test_agent_start_continue_override_beats_spec_fresh(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — spec explicitly says fresh; the CLI --continue maps to a
    # session_override="continue" that must win (precedence CLI > spec).
    spec = _write_spec(tmp_path, extra_spec="  session: fresh\n")
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
        session_override="continue",
        thread_factory=FakeThread,
    )
    # Assert
    assert captured["cfg"].claude.session == "continue"


def test_agent_start_fresh_override_beats_spec_continue(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — spec explicitly says continue; the CLI --fresh maps to a
    # session_override="fresh" that must win (precedence CLI > spec).
    spec = _write_spec(tmp_path, extra_spec="  session: continue\n")
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
        session_override="fresh",
        thread_factory=FakeThread,
    )
    # Assert
    assert captured["cfg"].claude.session == "fresh"


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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
    )
    # Assert
    with pytest.raises(RuntimeError, match="Failed to start"):
        call()


def _start_with_failing_runtime(spec: Path, registry: Registry) -> None:
    """Drive a real ``agent_start`` failure, swallowing the expected
    ``RuntimeError`` -- the raise itself is covered by
    ``test_agent_start_runtime_failure_raises_runtime_error``; these
    helpers only care about what got written to disk before it fired."""
    runtime = FakeRuntime(running=False, start_result=False)
    try:
        lc.agent_start(
            str(spec),
            registry=registry,
            runtime_factory=lambda _c: runtime,
            handover_mod=FakeHandover(),
            sleep_fn=_no_sleep,
            thread_factory=FakeThread,
        )
    except RuntimeError:
        pass


def test_agent_start_runtime_failure_persists_diag_file(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange -- a false-negative start whose only evidence must survive
    # past the raised exception (sac-agent-start-false-negative-tui-
    # registry-row-20260710): killing the tmux session is often the only
    # way to stop an agent with no registry row, which destroys a
    # not-yet-persisted pane capture forever.
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    spec = _write_spec(tmp_path)
    config = load_config(str(spec))
    # Act
    _start_with_failing_runtime(spec, registry)
    # Assert
    assert (state_dir_for_config(config) / "start_failure_diag.log").is_file()


def test_agent_start_runtime_failure_diag_names_the_reason(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    from scitex_agent_container.runtimes.tui_session import state_dir_for_config

    spec = _write_spec(tmp_path)
    config = load_config(str(spec))
    # Act
    _start_with_failing_runtime(spec, registry)
    # Assert
    diag_log = state_dir_for_config(config) / "start_failure_diag.log"
    assert "runtime.start() returned False" in diag_log.read_text()


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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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
        thread_factory=FakeThread,
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


def _stop_with_prune(
    tmp_path: Path,
    registry: Registry,
    env_save_restore,
    *,
    name: str,
    restart_block: str,
) -> Path:
    """Relocate the runtime base to tmp, register a real agent whose spec
    carries ``restart_block``, seed its runtime state dir, then run a
    terminal ``agent_stop(prune_runtime=True)``. Returns the state dir.
    """
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "rt"))
    ss = importlib.reload(
        importlib.import_module("scitex_agent_container._runners._session_state")
    )
    # Registered HERE, in the shared helper, so both callers are covered by
    # construction. Their old per-test `finally` blocks dropped the env var and
    # THEN reloaded, which re-derived DEFAULT_STATE_ROOT from the real $HOME
    # and left it there for every later test in the worker.
    env_save_restore.reload_after_restore(ss)
    spec = _write_spec(tmp_path, name=name, restart_block=restart_block)
    registry.add(name, str(spec), f"cld-{name}")
    state_dir = ss.state_dir_for(name)
    state_dir.mkdir(parents=True)
    (state_dir / "heartbeat.json").write_text("{}")
    lc.agent_stop(
        name,
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(),
        handover_mod=FakeHandover(),
        prune_runtime=True,
    )
    return state_dir


def test_agent_stop_prune_removes_ephemeral_runtime_dir(
    tmp_path: Path, registry: Registry, env_save_restore
) -> None:
    # Arrange
    restart_block = (
        "  restart:\n    policy: never\n    max_retries: 3\n    prune_on_stop: true\n"
    )
    # Act — never-policy agent that opted in via prune_on_stop.
    state_dir = _stop_with_prune(
        tmp_path,
        registry,
        env_save_restore,
        name="cap",
        restart_block=restart_block,
    )
    # Assert
    assert not state_dir.exists()


def test_agent_stop_prune_keeps_persistent_runtime_dir(
    tmp_path: Path, registry: Registry, env_save_restore
) -> None:
    # Arrange — persistent (always) agent must NEVER be pruned, even when
    # the terminal stop passes prune_runtime=True.
    restart_block = (
        "  restart:\n    policy: always\n    max_retries: 3\n    prune_on_stop: true\n"
    )
    # Act
    state_dir = _stop_with_prune(
        tmp_path,
        registry,
        env_save_restore,
        name="coord",
        restart_block=restart_block,
    )
    # Assert
    assert state_dir.exists()


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
        thread_factory=FakeThread,
    )
    # Assert
    assert ok is True and len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1


def test_agent_restart_clears_dead_session_marker(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — a runtime state dir holding a DEAD resume marker + history
    # (the production shape after a session aged out). PR #190's restart
    # left the dead uuid in the history to be re-resumed and re-crashed;
    # a plain restart must now clear it.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime_root = tmp_path / "rt"
    prev = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(runtime_root)
    try:
        from scitex_agent_container._runners import _session_id as sid

        state_dir = runtime_root / "alpha"
        sid.write_session_id(state_dir, "dead-uuid")
        # Act
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: FakeRuntime(start_result=True),
            sleep_fn=_no_sleep,
            handover_mod=FakeHandover(),
            thread_factory=FakeThread,
        )
        # Assert — the dead resume marker is gone so the restart is fresh.
        result = sid.read_session_id(state_dir)
    finally:
        if prev is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = prev
    assert result is None


def test_agent_restart_clears_dead_session_history(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — the dead uuid lives in the append-only history that the
    # runner's resume fallback would otherwise walk and re-resume.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime_root = tmp_path / "rt"
    prev = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(runtime_root)
    try:
        from scitex_agent_container._runners import _session_id as sid

        state_dir = runtime_root / "alpha"
        sid.write_session_id(state_dir, "dead-uuid")
        sid.write_session_id(state_dir, "dead-fork")
        # Act
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: FakeRuntime(start_result=True),
            sleep_fn=_no_sleep,
            handover_mod=FakeHandover(),
            thread_factory=FakeThread,
        )
        # Assert — the whole history is cleared so no dead uuid can be
        # re-resumed on the next start (the crash-loop is closed).
        history = sid.read_session_id_history(state_dir)
    finally:
        if prev is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = prev
    assert history == []


def test_agent_restart_backs_up_dead_session_history(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — clearing the dead history must preserve it as an audit
    # side-file, not silently destroy it.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime_root = tmp_path / "rt"
    prev = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(runtime_root)
    try:
        from scitex_agent_container._runners import _session_id as sid

        state_dir = runtime_root / "alpha"
        sid.write_session_id(state_dir, "dead-uuid")
        # Act
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: FakeRuntime(start_result=True),
            sleep_fn=_no_sleep,
            handover_mod=FakeHandover(),
            thread_factory=FakeThread,
        )
        # Assert
        backups = list(state_dir.glob("session_id_history.dead-*"))
    finally:
        if prev is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = prev
    assert len(backups) == 1


def test_agent_restart_unknown_raises(tmp_path: Path, registry: Registry) -> None:
    # Arrange — empty registry AND a resolver that finds no spec (a
    # genuinely unknown agent): both lookups must fail to raise.
    def _no_spec(_name: str) -> str:
        raise FileNotFoundError("ghost: no spec on the discovery chain")

    # Act
    call = lambda: lc.agent_restart(  # noqa: E731
        "ghost",
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(),
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
        config_resolver=_no_spec,
        thread_factory=FakeThread,
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


def test_agent_restart_no_row_falls_back_to_spec_and_starts(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — NO registry row for "alpha" (ad-hoc / pre-autorecord
    # launch); a resolver returns the real on-disk spec path so restart
    # falls back to the spec instead of hard-failing "not found".
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(start_result=True)
    # Act
    ok = lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
        config_resolver=lambda _name: str(spec),
        thread_factory=FakeThread,
    )
    # Assert — the spec-resolved start ran (fallback path reached the runtime).
    assert ok is True and len(runtime.start_calls) == 1


def test_agent_restart_no_row_force_stops_before_start(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — no registry row; a runtime whose stop() raises. The
    # fallback's force=True stop must swallow that and still reach start.
    spec = _write_spec(tmp_path)
    runtime = FakeRuntime(start_result=True)
    runtime.stop_should_raise = RuntimeError("session already gone")
    # Act
    ok = lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
        config_resolver=lambda _name: str(spec),
        thread_factory=FakeThread,
    )
    # Assert — force-stop tolerated the dead session and start still ran.
    assert ok is True and len(runtime.start_calls) == 1


def test_agent_restart_no_row_uses_default_resolver_discovery_chain(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — no registry row, NO injected resolver: the real default
    # ``resolve_config`` must find the spec under the standard
    # ``$HOME/.scitex/agent-container/agents/<name>/spec.yaml`` location.
    # ``_isolate_home`` (autouse) has already pointed HOME at tmp_path.
    # Chdir to a project-less dir so the new $SAC_AGENT_SCOPE ambiguity
    # rule sees ONLY the fleet registry (the sac worktree we'd otherwise
    # run from ships a tracked project-local registry, which would make
    # the scope ambiguous). A real fleet-from-non-project-cwd invocation
    # is exactly the single-registry case this test means to exercise.
    import os

    prev_cwd = os.getcwd()
    neutral = tmp_path / "_neutral_cwd"
    neutral.mkdir()
    os.chdir(str(neutral))
    agents_root = tmp_path / ".scitex" / "agent-container" / "agents"
    _write_spec(agents_root, name="beta")
    runtime = FakeRuntime(start_result=True)
    # Act — config_resolver left at its production default.
    try:
        ok = lc.agent_restart(
            "beta",
            registry=registry,
            runtime_factory=lambda _c: runtime,
            sleep_fn=_no_sleep,
            handover_mod=FakeHandover(),
            thread_factory=FakeThread,
        )
    finally:
        os.chdir(prev_cwd)
    # Assert — the default resolver found the spec and start ran.
    assert ok is True and len(runtime.start_calls) == 1


def test_agent_restart_passes_assume_yes_to_start_leg(
    tmp_path: Path, registry: Registry
) -> None:
    """A restart's inner ``agent_start`` MUST carry ``assume_yes=True``.

    Regression guard for the in-SIF broker 502 reproduced 2026-07-09: a
    ``sac agents restart <name>`` run inside an apptainer SIF reaches the
    LOCAL ``agent_restart`` → ``agent_start`` path; ``agent_start`` then
    brokers the start to the host's ``sac listen`` ``POST /agents``
    handler, which shells a FRESH ``sac agents start <name>`` subprocess.
    That subprocess re-runs the interactive refuse-without-``--yes`` gate,
    so unless the ORIGINAL restart's consent is threaded through as
    ``assume_yes`` the host refused itself with "refusing to start <name>
    without --yes/-y" → HTTP 502 — even though the restart was explicitly
    authorized. The host-side ``assume_yes`` → ``--yes`` argv plumbing is
    proven in ``test__agent_exec_subprocess.py``; this pins the missing
    link: ``agent_restart`` actually SETS ``assume_yes=True``.

    Real seam (no MagicMock): the ``_start.agent_start`` module attribute
    is swapped for a capture callable and restored in ``finally`` — the
    same save/restore-a-real-callable pattern the
    ``test_fire_forget_hook_swallows_run_hook_exceptions`` test uses.
    """
    # Arrange — a real on-disk spec so the pre-start stop/settle legs run
    # against a real FakeRuntime, then capture the kwargs the (swapped)
    # start leg receives.
    spec = _write_spec(tmp_path)
    from scitex_agent_container._lifecycle import _start as _start_mod

    captured: dict[str, Any] = {}
    original_start = _start_mod.agent_start

    def _capture_start(config_path: str, registry: Any = None, **kwargs: Any) -> bool:
        captured["config_path"] = config_path
        captured.update(kwargs)
        return True

    _start_mod.agent_start = _capture_start  # real callable, not Mock
    try:
        # Act
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: FakeRuntime(start_result=True),
            sleep_fn=_no_sleep,
            handover_mod=FakeHandover(),
            config_resolver=lambda _name: str(spec),
            thread_factory=FakeThread,
        )
    finally:
        _start_mod.agent_start = original_start
    # Assert — the restart's start leg asserted the already-given consent.
    assert captured.get("assume_yes") is True, captured


class _StaggeredRuntime(FakeRuntime):
    """Real fake whose ``is_running`` returns the next bool from a stage list.

    Models the apptainer teardown race: after ``stop()`` sends SIGTERM the
    container takes several ``is_running`` polls to actually exit. Used by
    ``test_agent_restart_waits_for_runtime_to_stop_before_starting`` to
    pin the bug shape (start was called WHILE the previous instance was
    still running) and verify the fix.
    """

    def __init__(self, *, stages: list[bool]) -> None:
        super().__init__(start_result=True)
        # Copy so the test can keep the list intact for assertions later.
        self._stages = list(stages)
        self.is_running_calls = 0
        self.was_running_at_start: bool | None = None

    def is_running(self, config: AgentConfig) -> bool:
        i = self.is_running_calls
        self.is_running_calls += 1
        # After the stage list is exhausted, stay at the last value (False
        # on a healthy teardown).
        if i < len(self._stages):
            return self._stages[i]
        return self._stages[-1] if self._stages else False

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        # Record whether the previous instance was STILL RUNNING at the
        # moment start() fired — this is the exact bug shape we're fixing.
        # Read directly off the stage list so the recording itself doesn't
        # consume a poll slot.
        i = self.is_running_calls
        if i == 0:
            self.was_running_at_start = self._stages[0] if self._stages else False
        else:
            idx = min(i - 1, len(self._stages) - 1)
            self.was_running_at_start = self._stages[idx]
        return super().start(config, **kwargs)


# ---------------------------------------------------------------------------
# agent_restart — wait for previous runtime to stop before starting.
#
# Bug shape (2026-06-07, operator-visible): ``agent_restart`` called
# ``runtime.stop()`` (which sends SIGTERM and returns immediately) then
# slept a fixed 2 s and called ``runtime.start()``. With apptainer, the
# old container's ``/home/agent`` overlay was still mounted when the
# new one booted ("destination is already in the mount point list"
# warning in stdout.log), and the old SDK's per-agent stdio MCP
# children (the standalone bun telegrammer poller) were still alive
# holding their PID lock file. The new bun child then hit
# ``acquireLock`` against the live old PID and exited 1; claude
# silently marked the MCP failed and never retried it. Symptom: the
# Telegram bot stopped responding after every ``sac agents restart``
# even though ``sac`` + Mermaid MCPs reloaded fine (no inter-instance
# lock for those).
#
# Fix: poll ``runtime.is_running`` until it returns False (with a
# bounded timeout) BEFORE calling ``runtime.start``. The fixed
# ``sleep_fn(2)`` is replaced by a real readiness gate. The cases
# below split the prior four-assertion test into one-assert tests so
# CI red names exactly which contract broke.
# ---------------------------------------------------------------------------


def _build_staggered_setup(
    tmp_path: Path, registry: Registry, *, stages: list[bool]
) -> _StaggeredRuntime:
    """Arrange helper for the staggered-teardown restart cases.

    Mirrors a realistic apptainer teardown (~0.5-2 s under healthy
    load): ``is_running`` returns ``True`` for the leading ``True``
    stages, then ``False``. Returns the staged runtime; callers issue
    the restart in their own ``# Act`` so each test keeps AAA markers
    on separate lines.
    """
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    return _StaggeredRuntime(stages=stages)


def _restart_alpha(runtime: _StaggeredRuntime, registry: Registry) -> bool:
    """Act helper: drive ``agent_restart`` against the staged runtime."""
    return lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
        thread_factory=FakeThread,
    )


def test_agent_restart_returns_true_after_waiting_for_previous_to_stop(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    runtime = _build_staggered_setup(
        tmp_path, registry, stages=[True, True, True, False]
    )
    # Act
    ok = _restart_alpha(runtime, registry)
    # Assert
    assert ok is True


def test_agent_restart_calls_runtime_start_exactly_once_after_waiting_for_stop(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    runtime = _build_staggered_setup(
        tmp_path, registry, stages=[True, True, True, False]
    )
    # Act
    _restart_alpha(runtime, registry)
    # Assert
    assert len(runtime.start_calls) == 1


def test_agent_restart_does_not_call_start_while_previous_runtime_is_still_running(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    runtime = _build_staggered_setup(
        tmp_path, registry, stages=[True, True, True, False]
    )
    # Act
    _restart_alpha(runtime, registry)
    # Assert — agent_restart must NOT have called runtime.start() while
    # runtime.is_running() was still True; that would re-trigger the
    # apptainer mount + telegrammer lock race.
    assert runtime.was_running_at_start is False


def test_agent_restart_polls_is_running_until_false(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    runtime = _build_staggered_setup(
        tmp_path, registry, stages=[True, True, True, False]
    )
    # Act
    _restart_alpha(runtime, registry)
    # Assert — must poll at least the number of "still running" stages
    # (4 polls covers 3 True samples + the final False).
    assert runtime.is_running_calls >= 4


# ---------------------------------------------------------------------------
# agent_restart — previous runtime that ignores SIGTERM past timeout.
#
# Loud-but-proceed semantics: a stuck container is rare, but silently
# spinning forever locks the operator out of restart entirely. We must
# (a) still return True, (b) still call start() on the new runtime, and
# (c) emit a WARN-level log naming the race so a future "telegrammer
# dropped after restart" recurrence is self-diagnosing from stdout.log.
# ---------------------------------------------------------------------------


# A previous runtime that will NOT die: ``is_running`` stays True forever and
# ``FakeRuntime`` never overrides ``agent_pid``, so the escalation has nothing
# to SIGKILL. This is the shape that produced the operator's 2026-07-14
# terminal:
#
#   WARN: previous runtime still running after 15.00s (SIGTERM ignored...);
#         proceeding to start anyway.
#   FAIL: duplicate session 'tui-neurovista' — agent already running.
#   Agent 'neurovista' restarted        <-- IT WAS NOT
#
# The gate PREDICTED the collision, proceeded into it, and the restart then
# reported success over an agent that was left DOWN. These cases used to
# assert exactly that behaviour ("returns True", "starts anyway"); they now
# assert its opposite. A stop that could not stop the thing must not report
# success and must not start a replacement that is guaranteed to collide.


def _build_unkillable_setup(
    tmp_path: Path, registry: Registry, caplog: Any
) -> _StaggeredRuntime:
    """Arrange helper: previous runtime whose ``is_running`` stays True
    forever and which cannot name a pid to kill; caplog routed to the
    escalation module so its WARN records are captured.
    """
    import logging as _logging

    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    caplog.set_level(
        _logging.WARNING,
        logger="scitex_agent_container._lifecycle._stop_escalate",
    )
    return _StaggeredRuntime(stages=[True])


def _restart_alpha_with_short_wait(
    runtime: _StaggeredRuntime, registry: Registry
) -> bool:
    """Act helper: bounded-timeout restart so the test is fast."""
    return lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=FakeHandover(),
        # Short timeout so the test is fast; ``_no_sleep`` makes the poll
        # loop spin as fast as Python allows.
        wait_for_stop_timeout_s=0.05,
        thread_factory=FakeThread,
    )


def test_agent_restart_raises_when_previous_runtime_will_not_exit(
    tmp_path: Path, registry: Registry, caplog: Any
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._stop_escalate import StopEscalationError

    runtime = _build_unkillable_setup(tmp_path, registry, caplog)
    # Act
    call = lambda: _restart_alpha_with_short_wait(runtime, registry)  # noqa: E731
    # Assert — it used to return True here, and the CLI printed "restarted".
    with pytest.raises(StopEscalationError):
        call()


def test_agent_restart_does_not_start_when_previous_runtime_will_not_exit(
    tmp_path: Path, registry: Registry, caplog: Any
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._stop_escalate import StopEscalationError

    runtime = _build_unkillable_setup(tmp_path, registry, caplog)
    # Act
    try:
        _restart_alpha_with_short_wait(runtime, registry)
    except StopEscalationError:
        pass
    # Assert — starting here is what caused the duplicate-session collision.
    assert len(runtime.start_calls) == 0


def test_agent_restart_warns_about_still_running_previous_runtime(
    tmp_path: Path, registry: Registry, caplog: Any
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._stop_escalate import StopEscalationError

    runtime = _build_unkillable_setup(tmp_path, registry, caplog)
    # Act
    try:
        _restart_alpha_with_short_wait(runtime, registry)
    except StopEscalationError:
        pass
    messages = " ".join(rec.getMessage() for rec in caplog.records)
    # Assert — a WARN log still names the SIGTERM-deaf runtime, so a
    # recurrence stays self-diagnosing from stdout.log.
    assert "SIGTERM" in messages


# ---------------------------------------------------------------------------
# agent_status
# ---------------------------------------------------------------------------


def test_agent_status_unknown_raises(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange — empty file registry AND an isolated empty state.db, so
    # neither the local registry nor the cross-host instances fallback
    # has a row for "ghost".
    # Act
    call = lambda: lc.agent_status(  # noqa: E731
        "ghost", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    with pytest.raises(RuntimeError, match="not found"):
        call()


def test_agent_status_resolves_remote_agent_from_instances_row(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange — a remote-dispatched agent has NO local file-registry
    # entry; its row lives only in the instances table (remote=1, peer
    # host, peer-resolved bound_port). Status must resolve it instead of
    # raising "not found".
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name="clew", host="spartan", bound_port=19123, remote=True, spawned_by="lead"
    )
    # Act
    result = lc.agent_status("clew", registry=registry)
    # Assert
    assert result["host"] == "spartan"


def test_agent_status_remote_row_reports_bound_port(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name="clew", host="spartan", bound_port=19123, remote=True, spawned_by="lead"
    )
    # Act
    result = lc.agent_status("clew", registry=registry)
    # Assert
    assert result["bound_port"] == 19123


def test_agent_status_remote_row_marks_remote_true(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name="clew", host="spartan", bound_port=19123, remote=True, spawned_by="lead"
    )
    # Act
    result = lc.agent_status("clew", registry=registry)
    # Assert
    assert result["remote"] is True


def test_agent_status_remote_row_reports_spawned_by(
    tmp_path: Path, registry: Registry, isolated_state_db: Path
) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name="clew", host="spartan", bound_port=19123, remote=True, spawned_by="lead"
    )
    # Act
    result = lc.agent_status("clew", registry=registry)
    # Assert
    assert result["spawned_by"] == "lead"


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


# ---------------------------------------------------------------------------
# agent_status — account field (operator request 4581).
#
# The autouse ``_isolate_home`` fixture points HOME at tmp_path, so an
# agent with no env override and no credentials.json there resolves to
# "unknown"; writing a real credentials.json + ~/.claude.json under HOME
# exercises the host-OAuth path; a spec.env override exercises the
# distinct-credential path. All real files, no mocks.
# ---------------------------------------------------------------------------


def test_agent_status_account_unknown_when_no_credentials(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — HOME (=tmp_path) has no credentials.json and the spec
    # carries no SAC_ANTHROPIC_API_KEY override.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["account"] == "unknown"


def test_agent_status_account_reports_host_oauth_email(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — real host OAuth files under HOME (=tmp_path).
    import json as _json

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".credentials.json").write_text(
        _json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "expiresAt": 9_999_999_999_000,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    (tmp_path / ".claude.json").write_text(
        _json.dumps({"oauthAccount": {"emailAddress": "shared@example.com"}})
    )
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["account"] == "shared@example.com"


def test_agent_status_account_reports_apikey_fingerprint_on_env_override(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — spec.apptainer.env supplies a distinct API key (the v3
    # loader promotes it into cfg.env). HOME OAuth (if any) must be
    # ignored in favour of the agent's own credential.
    spec = _write_spec(
        tmp_path,
        extra_spec=(
            "  apptainer:\n"
            "    image: /x.sif\n"
            "    binds: []\n"
            "    env:\n"
            "      SAC_ANTHROPIC_API_KEY: sk-ant-api03-AAAABBBB7777\n"
        ),
    )
    registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["account"] == "apikey:…7777"


def test_agent_status_account_unknown_when_config_load_fails(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — config path points at a non-existent YAML so load_config
    # raises; the account resolver must degrade to "unknown" (config is
    # None), never crash status.
    registry.add("alpha", str(tmp_path / "alpha" / "spec.yaml"), "cld-alpha")
    # Act
    result = lc.agent_status(
        "alpha", registry=registry, runtime_factory=lambda _c: FakeRuntime()
    )
    # Assert
    assert result["account"] == "unknown"


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
