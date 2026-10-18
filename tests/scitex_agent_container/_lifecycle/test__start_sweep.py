"""Tests for the self-cleaning start-path sweep (card
``infra-start-silent-noop-and-stale-rows-20261016``).

``sac store clean``/``tick`` previously ran only by hand, so dead
``instances`` rows accumulated until an operator looked. Every
``agent_start`` now runs the same host-scoped sweep best-effort BEFORE
the liveness verdict, and the verdict announcement reports what it
cleared. These tests pin: the sweep runs on the start path, a raising
sweeper never breaks the start, dry runs never sweep, and the
announcement names the cleared rows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._lifecycle._start_announce import (
    _announce_start_verdict,
)
from scitex_agent_container._lifecycle._start_sweep import sweep_before_start
from scitex_agent_container._lifecycle._verdict import LivenessVerdict
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig
from tests.scitex_agent_container._helpers.spec_authority import (
    establish_test_spec_authority,
)

# ---------------------------------------------------------------------------
# sweep_before_start unit contract
# ---------------------------------------------------------------------------


def test_sweep_returns_counters_from_the_injected_sweeper() -> None:
    assert sweep_before_start(sweep_fn=lambda: {"reboot-swept": 2}) == {
        "reboot-swept": 2
    }


def test_sweep_folds_a_raising_sweeper_to_empty() -> None:
    def _boom() -> dict:
        raise RuntimeError("store unreachable")

    assert sweep_before_start(sweep_fn=_boom) == {}


def test_sweep_folds_a_non_dict_result_to_empty() -> None:
    assert sweep_before_start(sweep_fn=lambda: ["reboot-swept"]) == {}  # type: ignore[return-value]


def test_sweep_drops_zero_counters_and_non_int_values() -> None:
    assert sweep_before_start(
        sweep_fn=lambda: {"reboot-swept": 0, "crashed": 3, "note": "x"}
    ) == {"crashed": 3}


def test_sweep_skips_entirely_on_dry_run() -> None:
    calls: list[None] = []

    def _recorder() -> dict:
        calls.append(None)
        return {"crashed": 1}

    assert sweep_before_start(dry_run=True, sweep_fn=_recorder) == {}
    assert calls == []


# ---------------------------------------------------------------------------
# agent_start wiring (hand-rolled fakes, same shape as
# test__start_retracts_marker.py)
# ---------------------------------------------------------------------------


class FakeRuntime:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.start_calls: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return self.running

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.start_calls.append(config)
        return True

    def stop(self, config: AgentConfig) -> None:
        pass

    def logs(self, config: AgentConfig, lines: int) -> str:
        return ""


class FakeThread:
    def __init__(self, *, target=None, args=(), daemon=False, **_kw) -> None:
        self.target = target

    def start(self) -> None:
        pass

    def join(self, timeout=None) -> None:
        return None

    def is_alive(self) -> bool:
        return False


class FakeHandover:
    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "fake-uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def push_pre_stop_snapshot(self, config: AgentConfig, payload=None) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


def _write_spec(tmp_path: Path, *, name: str = "alpha") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        f"  workdir: {tmp_path / 'work'}\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  claude:\n    model: sonnet\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  hooks:\n    pre_start: []\n    post_start: []\n"
        "    pre_stop: []\n    post_stop: []\n"
    )
    from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

    spec = agent_dir / "spec.yaml"
    spec.write_text(explicitize_yaml(body))
    return establish_test_spec_authority(spec)


def test_start_runs_the_sweep_before_the_verdict(
    pg_schema: str, tmp_path: Path
) -> None:
    spec = _write_spec(tmp_path)
    registry = Registry(registry_dir=tmp_path / "reg")
    calls: list[None] = []

    def _recorder() -> dict:
        calls.append(None)
        return {"pid_absent_at_sweep": 1}

    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(running=False),
        handover_mod=FakeHandover(),
        sleep_fn=lambda _s: None,
        instance_sweep_fn=_recorder,
    )
    assert len(calls) == 1


def test_start_on_dry_run_never_sweeps(
    pg_schema: str, tmp_path: Path
) -> None:
    spec = _write_spec(tmp_path)
    registry = Registry(registry_dir=tmp_path / "reg")
    calls: list[None] = []

    def _recorder() -> dict:
        calls.append(None)
        return {"pid_absent_at_sweep": 1}

    agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(running=False),
        handover_mod=FakeHandover(),
        sleep_fn=lambda _s: None,
        instance_sweep_fn=_recorder,
        dry_run=True,
    )
    assert calls == []


def test_start_survives_a_raising_sweeper(
    pg_schema: str, tmp_path: Path
) -> None:
    spec = _write_spec(tmp_path)
    registry = Registry(registry_dir=tmp_path / "reg")

    def _boom() -> dict:
        raise RuntimeError("store unreachable")

    ok = agent_start(
        str(spec),
        thread_factory=FakeThread,
        registry=registry,
        runtime_factory=lambda _c: FakeRuntime(running=False),
        handover_mod=FakeHandover(),
        sleep_fn=lambda _s: None,
        instance_sweep_fn=_boom,
    )
    assert bool(ok) is True


# ---------------------------------------------------------------------------
# The announcement names the cleared rows (caplog over the scitex logger)
# ---------------------------------------------------------------------------


def _fake_verdict(*, unknown: bool = True) -> LivenessVerdict:
    return LivenessVerdict(
        agent="alpha",
        verdict=("UNKNOWN" if unknown else "ALIVE"),
    )


def test_announce_names_swept_rows(caplog) -> None:
    with caplog.at_level(logging.INFO):
        _announce_start_verdict(
            _fake_verdict(), swept={"pid_absent_at_sweep": 2}
        )
    assert any(
        "sweep: cleared 2 stale instance row(s) (pid_absent_at_sweep=2)"
        in rec.getMessage()
        for rec in caplog.records
    )


def test_announce_stays_silent_on_empty_sweep(caplog) -> None:
    with caplog.at_level(logging.INFO):
        _announce_start_verdict(_fake_verdict(), swept={})
    assert not any(
        "stale instance row" in rec.getMessage() for rec in caplog.records
    )
