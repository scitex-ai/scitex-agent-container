"""Integration tests: the auth pre-flight wired into the restart lifecycle.

INCIDENT ``incident-agent-self-restart-one-way-20260712``. The pre-flight
guards BOTH stop→start paths:

  * ``sac agents restart`` (manual + listen-brokered external) →
    :func:`_lifecycle._stop.agent_restart`, which probes BEFORE its
    ``agent_stop``; and
  * ``sac agents start --force`` (the PR #628 detached self-restart
    bounce) → :func:`_lifecycle._start.agent_start`, which probes in its
    force branch BEFORE ``agent_stop``.

On a REJECTED successor the pre-flight raises
:class:`_restart_preflight.RestartPreflightAbort` and the running
container is LEFT UP (never stopped); on a healthy successor the restart
proceeds unchanged. The auth check is injected via each function's
``successor_auth_check`` seam (a real callable — no mocks); the probe
logic itself is unit-tested in ``test__restart_preflight.py``.

No mocks: real ``Registry`` + real on-disk spec + a recording runtime
double. AAA markers, descriptive names, one assertion each.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._restart_preflight import RestartPreflightAbort
from scitex_agent_container._state.registry import Registry


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


class _FakeRuntime:
    """Recording runtime double (is_running / start / stop).

    ``stop`` really stops it — i.e. ``is_running`` reads False afterwards.
    That is not a convenience: a runtime whose ``is_running`` stays True
    after a stop and a full SIGTERM grace is a runtime whose stop FAILED,
    and ``agent_restart``'s teardown gate now (correctly) refuses to start
    a replacement over such a survivor — it escalates to SIGKILL and, when
    it still cannot confirm the agent is down, raises (see
    ``_lifecycle/_stop_escalate.py``; the operator's 2026-07-14
    "Agent 'neurovista' restarted" over a DOWN agent is what that closes).
    The cases in this module are about the CREDENTIAL PRE-FLIGHT, so their
    runtime must model a HEALTHY teardown; a stop that silently does
    nothing would have them exercising the wedged-teardown path by
    accident.
    """

    def __init__(self, *, running: bool = False, start_result: bool = True) -> None:
        self.running = running
        self.start_result = start_result
        self.start_calls: list[Any] = []
        self.stop_calls: list[Any] = []

    def is_running(self, config: Any) -> bool:
        return self.running

    def start(self, config: Any, **_kw: Any) -> bool:
        self.start_calls.append(config)
        self.running = True
        return self.start_result

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


def _write_spec(tmp_path: Path, name: str = "alpha") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  apptainer:\n"
        "    image: /x.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: sonnet\n"
        "  health:\n"
        "    enabled: false\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
        "  hooks:\n"
        "    pre_start: []\n"
        "    post_start: []\n"
        "    pre_stop: []\n"
        "    post_stop: []\n"
    )
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    spec = agent_dir / "spec.yaml"
    # Red-start ruling 2026-07-21: every field explicit (body wins).
    spec.write_text(explicitize_yaml(body))
    return spec


def _raise_unusable(_arg: Any) -> None:
    """A real successor_auth_check that rejects (the incident class)."""
    raise RestartPreflightAbort("successor credential unusable (test)")


# ---------------------------------------------------------------------------
# agent_restart — the `sac agents restart` path
# ---------------------------------------------------------------------------


def test_agent_restart_raises_abort_on_unusable_successor(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — a live agent whose successor auth pre-flight will REJECT.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act
    ctx = pytest.raises(RestartPreflightAbort)
    # Assert
    with ctx:
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: runtime,
            sleep_fn=_no_sleep,
            handover_mod=_FakeHandover(),
            successor_auth_check=_raise_unusable,
        )


def test_agent_restart_leaves_running_container_up_on_abort(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act — swallow the abort; the behaviour under test is the NON-stop.
    try:
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: runtime,
            sleep_fn=_no_sleep,
            handover_mod=_FakeHandover(),
            successor_auth_check=_raise_unusable,
        )
    except RestartPreflightAbort:
        pass
    # Assert — the running container was NEVER stopped (left UP).
    assert runtime.stop_calls == []


def test_agent_restart_launches_no_successor_on_abort(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act
    try:
        lc.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: runtime,
            sleep_fn=_no_sleep,
            handover_mod=_FakeHandover(),
            successor_auth_check=_raise_unusable,
        )
    except RestartPreflightAbort:
        pass
    # Assert — no dead successor was launched.
    assert runtime.start_calls == []


def test_agent_restart_healthy_successor_still_stops_then_starts(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — the pre-flight passes (returns None): normal restart proceeds.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act
    ok = lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=_FakeHandover(),
        successor_auth_check=lambda _cp: None,
    )
    # Assert — a healthy pre-flight must not disturb the stop→start.
    assert ok is True and len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1


def test_agent_restart_runs_preflight_before_the_stop(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — record ordering: the check must fire BEFORE the stop.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    order: list[str] = []

    class _OrderRuntime(_FakeRuntime):
        def stop(self, config: Any) -> None:
            order.append("stop")
            super().stop(config)

    runtime = _OrderRuntime(running=True, start_result=True)
    # Act
    lc.agent_restart(
        "alpha",
        registry=registry,
        runtime_factory=lambda _c: runtime,
        sleep_fn=_no_sleep,
        handover_mod=_FakeHandover(),
        successor_auth_check=lambda _cp: order.append("preflight"),
    )
    # Assert
    assert order[0] == "preflight"


# ---------------------------------------------------------------------------
# agent_start force — the `sac agents start --force` (PR #628 self-restart)
# ---------------------------------------------------------------------------


def test_agent_start_force_raises_abort_on_unusable_successor(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — a live agent, force-restart, pre-flight will REJECT.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act
    ctx = pytest.raises(RestartPreflightAbort)
    # Assert
    with ctx:
        lc.agent_start(
            str(spec),
            registry=registry,
            force=True,
            runtime_factory=lambda _c: runtime,
            handover_mod=_FakeHandover(),
            sleep_fn=_no_sleep,
            liveness_verifier=lambda _cfg, _rt: True,
            successor_auth_check=_raise_unusable,
        )


def test_agent_start_force_leaves_running_container_up_on_abort(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act — swallow the abort; the behaviour under test is the NON-stop.
    try:
        lc.agent_start(
            str(spec),
            registry=registry,
            force=True,
            runtime_factory=lambda _c: runtime,
            handover_mod=_FakeHandover(),
            sleep_fn=_no_sleep,
            liveness_verifier=lambda _cfg, _rt: True,
            successor_auth_check=_raise_unusable,
        )
    except RestartPreflightAbort:
        pass
    # Assert — the live container was NEVER stopped (self-restart left it UP).
    assert runtime.stop_calls == []


def test_agent_start_force_healthy_successor_still_restarts(
    tmp_path: Path, registry: Registry
) -> None:
    # Arrange — pre-flight passes: a normal force-restart must still work.
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _FakeRuntime(running=True, start_result=True)
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        force=True,
        runtime_factory=lambda _c: runtime,
        handover_mod=_FakeHandover(),
        sleep_fn=_no_sleep,
        liveness_verifier=lambda _cfg, _rt: True,
        successor_auth_check=lambda _cfg: None,
    )
    # Assert — healthy pre-flight preserves the force stop→start.
    assert len(runtime.stop_calls) == 1 and len(runtime.start_calls) == 1
