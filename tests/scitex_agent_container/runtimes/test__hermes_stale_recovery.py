from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container.runtimes import _hermes_stale_recovery as recovery
from scitex_agent_container.runtimes._runtime_control import read_control_state


def _config(tmp_path: Path):
    return SimpleNamespace(
        name="scholar",
        harness="hermes",
        runtime="tui",
        model="qwen38-27b",
        engine_key="qwen38-27b",
        config_path=str(tmp_path / "scholar.yaml"),
        autonomous=SimpleNamespace(
            enabled=True,
            idle_kick_after_s=600,
            kick_text="Continue.",
        ),
        claude=SimpleNamespace(
            provider=SimpleNamespace(base_url="http://gateway.test:18772/v1")
        ),
    )


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_only_the_exact_hermes_breaker_error_is_a_latch():
    # Arrange
    unrelated = "Provider request failed after 5 attempts"
    exact = "Provider has been unresponsive (no response received) for 5 consecutive stale attempts — aborting"
    # Act
    observed = (recovery.stale_latch(unrelated), recovery.stale_latch(exact))
    # Assert
    assert observed[0] is None and observed[1][0] == 5


def test_health_requires_free_capacity_when_gateway_exposes_admission(tmp_path):
    # Arrange
    config = _config(tmp_path)
    payload = {
        "status": "ok",
        "members": [
            {"active": True, "in_flight": 1, "queued": 1, "capacity": 2}
        ],
    }
    # Act
    available = recovery.provider_has_capacity(
        config, urlopen=lambda *_args, **_kwargs: _Response(payload)
    )
    # Assert
    assert available is False


def test_health_accepts_an_active_member_with_immediate_capacity(tmp_path):
    # Arrange
    config = _config(tmp_path)
    payload = {
        "status": "ok",
        "members": [
            {"active": True, "in_flight": 1, "queued": 0, "capacity": 2}
        ],
    }
    # Act
    available = recovery.provider_has_capacity(
        config, urlopen=lambda *_args, **_kwargs: _Response(payload)
    )
    # Assert
    assert available is True


def test_recovery_rebinds_same_model_and_provider_without_global_write(tmp_path):
    # Arrange
    config = _config(tmp_path)
    # Act
    command = recovery.recovery_command(config)
    # Assert
    assert command == "/model qwen38-27b --provider sac-qwen38-27b --session"


def test_recovery_tick_preserves_session_context_and_incarnation(tmp_path):
    # Arrange: these are the three durable identities the adapter must not
    # replace or delete. The context sentinel represents Hermes-private
    # session/context state.db, never the shared SciTeX store.
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    sentinels = {
        "session_id": "persistent-session\n",
        "instance_id": "persistent-incarnation\n",
        "home/.hermes/state.db": "persistent-context\n",
    }
    for relative, body in sentinels.items():
        path = state_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    calls = []
    pane = "Provider has been unresponsive for 5 consecutive stale attempts"
    # Act
    latched = recovery.recovery_tick(
        config,
        capture=lambda: pane,
        pause=lambda: calls.append("pause") or True,
        probe=lambda _config: True,
        recover=lambda: calls.append("same-session-rebind") or True,
        now=lambda: 42.0,
        state_dir=state_dir,
    )
    fingerprint = recovery.recovery_tick(
        config,
        capture=lambda: pane,
        pause=lambda: (_ for _ in ()).throw(AssertionError("paused twice")),
        probe=lambda _config: True,
        recover=lambda: calls.append("same-session-rebind") or True,
        now=lambda: 43.0,
        previous_fingerprint=latched,
        state_dir=state_dir,
    )
    # Assert
    assert (
        bool(fingerprint),
        calls,
        {key: (state_dir / key).read_text(encoding="utf-8") for key in sentinels},
        read_control_state(state_dir)["turn_admission"],
    ) == (
        True,
        ["pause", "same-session-rebind"],
        sentinels,
        "recovering",
    )


def test_one_observed_latch_can_trigger_only_one_recovery(tmp_path):
    # Arrange
    config = _config(tmp_path)
    pane = "Provider has been unresponsive for 5 consecutive stale attempts"
    recovered = []
    first = recovery.recovery_tick(
        config,
        capture=lambda: pane,
        pause=lambda: True,
        recover=lambda: recovered.append(1) or True,
        state_dir=tmp_path,
    )
    recovered_token = recovery.recovery_tick(
        config,
        capture=lambda: pane,
        pause=lambda: (_ for _ in ()).throw(AssertionError("paused twice")),
        probe=lambda _config: True,
        recover=lambda: recovered.append(1) or True,
        previous_fingerprint=first,
        state_dir=tmp_path,
    )
    # Act
    second = recovery.recovery_tick(
        config,
        capture=lambda: pane,
        pause=lambda: (_ for _ in ()).throw(AssertionError("paused twice")),
        probe=lambda _config: (_ for _ in ()).throw(AssertionError("re-probed")),
        recover=lambda: (_ for _ in ()).throw(AssertionError("recovered twice")),
        previous_fingerprint=recovered_token,
        state_dir=tmp_path,
    )
    # Assert
    assert second == recovered_token and recovered == [1]


def test_monitor_natural_exit_clears_persisted_latch(tmp_path):
    # Arrange
    config = _config(tmp_path)
    runtime = SimpleNamespace(session_name=lambda _config: "sac-scholar")
    mux = SimpleNamespace(exists=lambda _session: False)
    recovery.write_control_state(
        tmp_path,
        {
            "turn_admission": "stale_latched",
            "detail": "old marker",
            "observed_at": 1.0,
        },
    )
    # Act
    recovery._run_monitor_loop(
        config,
        runtime=runtime,
        mux=mux,
        wait=lambda _seconds: False,
        state_dir=tmp_path,
    )
    # Assert
    marker = read_control_state(tmp_path)
    assert (marker["turn_admission"], marker["detail"]) == (
        "ready",
        "owning Hermes TUI session ended; recovery observer stopped",
    )


def test_monitor_pauses_periodic_model_wakeup_once_and_only_observes(tmp_path):
    # Arrange: the monitor's lifecycle is finite because the fake mux reports
    # the pane gone. Cheap pane observation must not synthesize model turns.
    config = _config(tmp_path)
    calls = []

    class Runtime:
        @staticmethod
        def session_name(_config):
            return "tui-scholar"

        @staticmethod
        def suspend_autonomous_turns(_config):
            calls.append(("control", "/heartbeat pause"))
            return True

        @staticmethod
        def recover_turn_admission(_config):
            raise AssertionError("no stale provider latch")

    exists = iter((True, True, True, False))
    mux = SimpleNamespace(
        exists=lambda _session: next(exists),
        capture_content=lambda _session: "healthy pane",
    )
    # Act
    recovery._run_monitor_loop(
        config,
        runtime=Runtime(),
        mux=mux,
        wait=lambda _seconds: False,
        state_dir=tmp_path,
    )
    # Assert
    assert calls == [("control", "/heartbeat pause")]


def test_unrelated_live_pid_is_not_owned_by_recovery_adapter(tmp_path):
    # Arrange
    config = _config(tmp_path)
    # This pytest process exists, but its argv cannot identify it as the
    # recovery module for this non-existent spec path.
    # Act
    owned = recovery._owns_monitor_process(os.getpid(), config.config_path)
    # Assert
    assert owned is False
