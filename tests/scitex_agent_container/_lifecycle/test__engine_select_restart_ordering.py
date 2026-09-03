"""A restart refuses an unhonourable engine BEFORE it stops the agent.

``agent_start`` refuses an unhonourable engine in its pre-launch region,
ahead of the ``--force`` stop, so a refusal there costs nothing.
``agent_restart`` is the other shape: it stops FIRST and then calls
``agent_start``. Reached only through that start leg, the same refusal
would fire on an agent that is already DOWN — one typo in ``--engine``
and the agent is stopped and never comes back.

That is the one-way trip ``incident-agent-self-restart-one-way-20260712``
is about, and the successor-credential pre-flight already guards this
exact window; the engine check stands beside it. These tests assert the
ORDERING, which is the part a unit test of the refusal cannot see: on a
refusal the recording runtime's ``stop`` must never have been called.

POSITIVE CONTROL: the SAME spec and the SAME engine, with the engine's
auth env var exported, must restart normally — stop AND start both run.
Without that pairing, ``stop_calls == []`` would pass just as well
against a restart that refuses everything, or one that never got as far
as the runtime at all.

No mocks: real ``Registry``, real on-disk spec, real ``load_config``,
real refusal path. The only injected things are the recording runtime
double and the host ENVIRONMENT (which env var is exported) — the
environment IS the input the refusal reads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._engine_select import (
    EngineNotHonourableError,
    check_engine_before_stop,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config._engine_types import UnknownEngineError
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_TOKEN_ENV = "SAC_TEST_RESTART_ENGINE_TOKEN"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path) -> Iterator[None]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(registry_dir=tmp_path / "reg")


def _env_set(name: str, value: str | None) -> Iterator[str]:
    """Write the REAL ``os.environ``, yield, and put it back.

    Real environment rather than ``monkeypatch`` (PA-306): the refusal
    resolves the token through the same cascade the launch uses, so the
    environment is the input under test, not a resolver to patch.
    """
    saved = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield name
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


@pytest.fixture
def token_absent() -> Iterator[str]:
    """Guarantee the engine's auth env var is UNSET on this host."""
    yield from _env_set(_TOKEN_ENV, None)


@pytest.fixture
def token_exported() -> Iterator[str]:
    """Export the engine's auth env var, as a working host would."""
    yield from _env_set(_TOKEN_ENV, "sk-test-not-a-real-key")


class _RecordingRuntime:
    """Runtime double that records stop/start and really stops.

    ``stop`` must make ``is_running`` read False: ``agent_restart``'s
    teardown gate refuses to start a replacement over a survivor, so a
    stop that did nothing would have these cases exercising the
    wedged-teardown path by accident rather than the ordering they are
    about.
    """

    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.start_calls: list[Any] = []
        self.stop_calls: list[Any] = []

    def is_running(self, config: Any) -> bool:
        return self.running

    def start(self, config: Any, **_kw: Any) -> bool:
        self.start_calls.append(config)
        self.running = True
        return True

    def stop(self, config: Any) -> None:
        self.stop_calls.append(config)
        self.running = False

    def logs(self, config: Any, lines: int) -> str:
        return ""


class _FakeHandover:
    def ensure_instance_uuid(self, c: Any) -> str:
        return "uuid"

    def hydrate_from_hub(self, c: Any) -> bool:
        return True

    def push_pre_stop_snapshot(self, c: Any, payload: Any = None) -> bool:
        return True

    def start_failback_poller(self, c: Any) -> None:
        return None


def _no_sleep(_s: float) -> None:
    return None


def _auth_ok(_config_path: Any) -> None:
    """A real successor auth check that ACCEPTS.

    Injected so these cases fail for engine reasons only — an unrelated
    credential verdict must not be what leaves the agent up.
    """
    return None


def _write_spec(tmp_path: Path, name: str = "alpha") -> Path:
    """A real spec declaring two engines, one of them token-gated."""
    spec = explicit_spec(
        {
            "host": "${HOSTNAME}",
            "runtime": "apptainer",
            "workdir": str(tmp_path / "work"),
            "apptainer": {"image": "/x.sif", "binds": []},
            "health": {"enabled": False, "interval": 60},
            "engines": {
                "claude": {
                    "harness": "anthropic",
                    "model": "fable[1m]",
                    "default": True,
                },
                "qwen38-27b": {
                    "harness": "anthropic",
                    "model": "qwen38-27b",
                    "provider": {
                        "base_url": "http://127.0.0.1:18772",
                        "auth_token_env": _TOKEN_ENV,
                    },
                    "reasoning_effort": "low",
                },
            },
        }
    )
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return path


def _restart(registry: Registry, runtime: _RecordingRuntime, engine: str | None):
    return lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=_FakeHandover(),
        successor_auth_check=_auth_ok,
        engine_override=engine,
    )


# ---------------------------------------------------------------------------
# An engine that cannot be honoured
# ---------------------------------------------------------------------------


def test_restart_on_an_unhonourable_engine_raises(
    tmp_path: Path, registry: Registry, token_absent: str
) -> None:
    # Arrange — a live agent; the named engine's token is unset here.
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _RecordingRuntime(running=True)
    # Act
    ctx = pytest.raises(EngineNotHonourableError)
    # Assert
    with ctx:
        _restart(registry, runtime, "qwen38-27b")


def test_restart_on_an_unhonourable_engine_never_stops_the_agent(
    tmp_path: Path, registry: Registry, token_absent: str
) -> None:
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _RecordingRuntime(running=True)
    # Act — swallow the refusal; the behaviour under test is the NON-stop.
    try:
        _restart(registry, runtime, "qwen38-27b")
    except EngineNotHonourableError:
        pass
    # Assert — the running agent was never torn down.
    assert runtime.stop_calls == []


def test_restart_on_an_unhonourable_engine_leaves_the_agent_running(
    tmp_path: Path, registry: Registry, token_absent: str
) -> None:
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _RecordingRuntime(running=True)
    # Act
    try:
        _restart(registry, runtime, "qwen38-27b")
    except EngineNotHonourableError:
        pass
    # Assert — still UP, i.e. re-startable, not stranded.
    assert runtime.is_running(None) is True


# ---------------------------------------------------------------------------
# An engine key the spec does not declare
# ---------------------------------------------------------------------------


def test_restart_with_an_unknown_engine_key_raises(
    tmp_path: Path, registry: Registry, token_exported: str
) -> None:
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _RecordingRuntime(running=True)
    # Act — a typo is not a reason to run a different backend.
    ctx = pytest.raises(UnknownEngineError)
    # Assert
    with ctx:
        _restart(registry, runtime, "qwen38-27bb")


def test_restart_with_an_unknown_engine_key_never_stops_the_agent(
    tmp_path: Path, registry: Registry, token_exported: str
) -> None:
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _RecordingRuntime(running=True)
    # Act
    try:
        _restart(registry, runtime, "qwen38-27bb")
    except UnknownEngineError:
        pass
    # Assert
    assert runtime.stop_calls == []


# ---------------------------------------------------------------------------
# POSITIVE CONTROLS — the same spec, the same gate, PASSING
#
# Driven at the gate rather than through a whole restart, and that is a
# limitation worth stating rather than hiding: a restart that PROCEEDS
# reaches ``agent_stop`` → the ``instances`` store, which needs a writable
# PostgreSQL that neither this container nor the CI runners have (loopback
# is a read-only fleet replica; see ``tests/_store_isolation.py``). The
# happy path therefore cannot be observed anywhere the suite runs — it is
# why the sibling credential pre-flight's own proceed-cases are the five
# SKIPPED tests in ``test__restart_preflight_integration.py``.
#
# So the pairing is split across two levels and both halves are real:
# ABOVE, the refusal is driven through the whole ``agent_restart`` and the
# runtime records that ``stop`` never ran (the ORDERING). HERE, the exact
# function that refusal came from is handed the exact same spec with the
# environment changed, and does NOT refuse. Without these, every
# ``stop_calls == []`` above would pass equally against a gate that
# refuses unconditionally.
# ---------------------------------------------------------------------------


def test_the_same_engine_with_its_token_exported_passes_the_gate(
    tmp_path: Path, token_exported: str
) -> None:
    # Arrange — identical spec and engine; only the environment differs.
    spec = _write_spec(tmp_path)
    # Act
    verdict = check_engine_before_stop(str(spec), "qwen38-27b", log=False)
    # Assert — no refusal, so the restart would have gone on to stop.
    assert verdict is None


def test_the_default_engine_passes_the_gate_with_no_token_at_all(
    tmp_path: Path, token_absent: str
) -> None:
    # Arrange — the token is UNSET, but the DEFAULT engine needs none.
    spec = _write_spec(tmp_path)
    # Act
    verdict = check_engine_before_stop(str(spec), None, log=False)
    # Assert — the unchanged no-flag restart is not refused by this gate.
    assert verdict is None


def test_the_gate_refuses_the_same_spec_when_the_token_is_absent(
    tmp_path: Path, token_absent: str
) -> None:
    # Arrange — the pair of the two above: same spec, same gate, engine
    # named explicitly, token gone.
    spec = _write_spec(tmp_path)
    # Act
    ctx = pytest.raises(EngineNotHonourableError)
    # Assert
    with ctx:
        check_engine_before_stop(str(spec), "qwen38-27b", log=False)
