"""``agent_start``'s already-running gate — the UNFALSIFIABLE ROW, broken.

NO MOCKS. Real on-disk YAML, the real file-based ``Registry``, real runtime
fakes implementing the production contract (the same seams
``test_lifecycle.py`` uses), and the real ``_verdict`` decision rule.

THE BUG
-------
``agent_start`` gated its no-op on ``registry.exists AND runtime.is_running AND
<instances row>`` — three PID/row-shaped proxies AND-ed into one bit. A wedged,
auth-dead or deaf agent whose process still exists satisfies all three, so
``sac agents start`` printed *"already running. No-op."* forever. Nothing in the
gate could ever come back and say "actually, no": the row was UNFALSIFIABLE.
The only escape was ``--force --fresh``, which DESTROYS the session — so the
remedy for *"I am not sure this is alive"* was *"kill it"*.

THE FIX
-------
Only an ALIVE verdict — POSITIVE evidence that something observed the agent
alive — pins the no-op. UNKNOWN falls through to a real start, which is safe
because **starting is not destroying**: the runtimes carry their own
duplicate-session guard (``TuiSessionRuntime.start``: *"if the session exists and
not force: return True"*), so an agent that turns out to be alive after all is
no-op'd at the runtime rather than relaunched over.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._start_verdict import resolve_start_verdict
from scitex_agent_container._lifecycle._verdict import (
    ALIVE,
    DEAD,
    SOURCE_DELIVERY,
    SOURCE_PROCESS,
    UNKNOWN,
    Signal,
    decide,
)
from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._state.registry import Registry


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path) -> Iterator[None]:
    """Redirect ``$HOME`` so ``Path.home()``-derived paths land in tmp_path.

    Without this the resolvers read (and the start path could WRITE) the real
    fleet runtime dir. Note the resolvers deliberately compute their roots at
    CALL time, never as import-time constants — an import-time
    ``Path.home()`` constant cannot be redirected by this fixture at all.
    """
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


class _Runtime:
    """A real runtime implementing the production seam contract."""

    def __init__(self, *, running: bool, start_result: bool = True) -> None:
        self._running = running
        self._start_result = start_result
        self.start_calls: list = []
        self.stop_calls: list = []

    def is_running(self, config) -> bool:
        return self._running

    def start(self, config, **kwargs) -> bool:
        self.start_calls.append(kwargs)
        return self._start_result

    def stop(self, config) -> bool:
        self.stop_calls.append(config.name)
        return True

    def logs(self, config, lines: int = 50) -> str:
        return ""


class _Handover:
    """The real handover seam surface."""

    def ensure_instance_uuid(self, config) -> None:
        return None

    def hydrate_from_hub(self, config) -> None:
        return None

    def start_failback_poller(self, config) -> None:
        return None


class _Cfg:
    def __init__(self, name: str = "alpha") -> None:
        self.name = name
        self.runtime = "tui"


def _write_spec(tmp_path: Path, name: str = "alpha") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
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
        "    policy: never\n"
        "    max_retries: 3\n"
    )
    return spec


def _no_sleep(_seconds: float) -> None:
    return None


# --------------------------------------------------------------------------
# resolve_start_verdict — the legacy bool seam, mapped faithfully.
# --------------------------------------------------------------------------


def test_legacy_verifier_all_three_signals_true_is_alive(tmp_path, registry):
    """Back-compat: the old no-op condition still yields ALIVE (still no-ops)."""
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _Runtime(running=True)
    # Act
    verdict = resolve_start_verdict(
        _Cfg(), runtime, registry=registry, liveness_verifier=lambda _c, _r: True
    )
    # Assert
    assert verdict.verdict == ALIVE


def test_legacy_verifier_saying_no_is_unknown_never_dead(tmp_path, registry):
    """A bool cannot express death — and it does not need to.

    UNKNOWN and DEAD lead to the same NON-destructive place here: a real start.
    """
    # Arrange
    registry.add("alpha", str(_write_spec(tmp_path)), "cld-alpha")
    runtime = _Runtime(running=True)
    # Act
    verdict = resolve_start_verdict(
        _Cfg(), runtime, registry=registry, liveness_verifier=lambda _c, _r: False
    )
    # Assert
    assert verdict.verdict == UNKNOWN


def test_legacy_verifier_missing_registry_row_is_unknown(tmp_path, registry):
    # Arrange — registry has NO row for this agent.
    runtime = _Runtime(running=True)
    # Act
    verdict = resolve_start_verdict(
        _Cfg(), runtime, registry=registry, liveness_verifier=lambda _c, _r: True
    )
    # Assert
    assert verdict.verdict == UNKNOWN


# --------------------------------------------------------------------------
# THE FIX: an UNKNOWN agent is recoverable WITHOUT --force --fresh.
# --------------------------------------------------------------------------


def test_an_unknown_agent_is_started_rather_than_no_opped(tmp_path, registry):
    """THE unfalsifiable-row regression.

    The agent looks running to every proxy (registry row present, runtime says
    up) but NOTHING can vouch for it — the verdict is UNKNOWN. The old gate
    no-op'd here forever. It must now start.
    """
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _Runtime(running=True, start_result=True)
    unknown = decide(
        "alpha", [Signal(SOURCE_PROCESS, UNKNOWN, "tmux probe FAILED")]
    )
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=_no_sleep,
        verdict_override=unknown,
    )
    # Assert — it started; it did NOT silently no-op.
    assert len(runtime.start_calls) == 1


def test_an_unknown_agent_is_started_without_force(tmp_path, registry):
    """And it does so WITHOUT --force — i.e. without stopping anything."""
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _Runtime(running=True, start_result=True)
    unknown = decide(
        "alpha", [Signal(SOURCE_PROCESS, UNKNOWN, "tmux probe FAILED")]
    )
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=_no_sleep,
        verdict_override=unknown,
    )
    # Assert — nothing was torn down. Starting is not destroying.
    assert runtime.stop_calls == []


def test_a_dead_agent_is_started(tmp_path, registry):
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _Runtime(running=False, start_result=True)
    dead = decide("alpha", [Signal(SOURCE_PROCESS, DEAD, "no session")])
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=_no_sleep,
        verdict_override=dead,
    )
    # Assert
    assert len(runtime.start_calls) == 1


# --------------------------------------------------------------------------
# ...and the safety property it must NOT lose: an ALIVE agent still no-ops.
# --------------------------------------------------------------------------


def test_an_alive_agent_still_no_ops(tmp_path, registry):
    """Positive evidence of life still pins the no-op — we did not just delete it."""
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _Runtime(running=True, start_result=True)
    alive = decide(
        "alpha", [Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber")]
    )
    # Act
    lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=_no_sleep,
        verdict_override=alive,
    )
    # Assert — never relaunched over a live agent.
    assert runtime.start_calls == []


def test_an_alive_agent_no_op_returns_success(tmp_path, registry):
    # Arrange
    spec = _write_spec(tmp_path)
    registry.add("alpha", str(spec), "cld-alpha")
    runtime = _Runtime(running=True, start_result=True)
    alive = decide(
        "alpha", [Signal(SOURCE_DELIVERY, ALIVE, "1 live inbox subscriber")]
    )
    # Act
    ok = lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _c: runtime,
        handover_mod=_Handover(),
        sleep_fn=_no_sleep,
        verdict_override=alive,
    )
    # Assert
    assert ok is True
