"""TUI turn bridge (``runtimes._tui_turn_bridge``): start/stop of the bridge process, on_turn and refusal text.

Real seams only (PA-306 no-mocks); see test__tui_turn_bridge.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml
from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _PID,
    _PORT,
    _gate_says_free,
)


# ---------------------------------------------------------------------------
# Launcher — real spawn recorder, real PID file
# ---------------------------------------------------------------------------
def test_start_turn_bridge_noop_without_resolved_port(tmp_path: Path) -> None:
    # Arrange — an unresolved 'auto' port; spawn must never be invoked.
    def must_not_spawn(*_a, **_k):
        raise AssertionError("spawn must not run without a resolved a2a port")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port="auto"),
        name="figrecipe",
        config_path=str(tmp_path / "spec.yaml"),
    )
    # Act
    pid = bridge.start_turn_bridge(config, spawn=must_not_spawn)
    # Assert
    assert pid is None


def test_start_turn_bridge_passes_resolved_port_to_spawn(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a resolved port + config_path; record the spawn argv.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert str(_PORT) in recorded["argv"]


def test_start_turn_bridge_passes_effective_cards_store_to_child(
    tmp_path: Path, isolated_home: Path, env_save_restore
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(_argv, **kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(pid=_PID)

    env_save_restore.set("SCITEX_STORE_DSN", "postgresql://wrong-shell:55432/store")
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT),
        apptainer=SimpleNamespace(raw_args=[]),
        env={"SCITEX_STORE_DSN": "postgresql://cards-store:55432/status"},
        labels={},
        name="figrecipe",
        config_path=str(spec),
    )
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["env"]["SCITEX_STORE_DSN"] == (
        "postgresql://cards-store:55432/status"
    )


def test_start_turn_bridge_returns_spawned_pid(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert
    assert pid == _PID


def test_stop_turn_bridge_noop_when_no_pid_file(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — no bridge was ever started for this agent.
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="never-started", config_path=""
    )
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is False


# ---------------------------------------------------------------------------
# _build_on_turn — the inject callback (runtime DI seam, no tmux)
# ---------------------------------------------------------------------------
def test_build_on_turn_passes_text_and_wait_ready_false() -> None:
    # Arrange — a recording runtime whose send_turn reports delivered.
    seen: list = []

    def fake_send_turn(config, text, wait_ready):
        seen.append((text, wait_ready))
        return True

    runtime = SimpleNamespace(send_turn=fake_send_turn)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="a"), runtime=runtime)
    # Act
    on_turn("wake up")
    # Assert
    assert seen == [("wake up", False)]


def test_build_on_turn_preserves_native_visible_delivery_receipt() -> None:
    # Arrange
    receipt = SimpleNamespace(
        status="steered", visibility="session.inflight.corrections"
    )
    seen = []
    runtime = SimpleNamespace(
        send_visible_turn=lambda config, text, **kwargs: (
            seen.append((text, kwargs)) or receipt
        )
    )
    on_turn = bridge._build_on_turn(SimpleNamespace(name="a"), runtime=runtime)

    # Act
    result = on_turn("wake up", visible_delivery_id="n_visible")

    # Assert
    assert (result, seen) == (
        receipt,
        [
            (
                "wake up\n<!-- delivery:n_visible -->",
                {
                    "visible_delivery_id": "n_visible",
                    "delivery_mode": "steer",
                },
            )
        ],
    )


def test_build_on_turn_raises_when_session_absent() -> None:
    # Arrange — send_turn reports the session does not exist (returns False).
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: False)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ghost"), runtime=runtime)
    # Act
    # Assert
    with pytest.raises(RuntimeError):
        on_turn("wake up")


# A refusal that explains itself and stops leaves the sender nowhere to go.
# scitex-hub hit exactly that on 2026-09-07: three identical 502s, then it
# reported "cannot reach sac" — correct against the old contract, and a system
# failure anyway. These pin the two things a sender cannot act without.


@pytest.fixture()
def refusal_message() -> str:
    """The RuntimeError text a REFUSED turn produces.

    Built once here so each test below asserts exactly one thing (STX-TQ007):
    a `pytest.raises` block counts as an assertion, so raising and inspecting
    in the same test is two.
    """
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: False)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ghost"), runtime=runtime)
    try:
        on_turn("wake up")
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("a refused turn must raise; it did not")


def test_the_refusal_says_NOTHING_WAS_QUEUED(refusal_message: str) -> None:
    # Arrange — the text a refused turn hands the sender.
    message = refusal_message

    # Act
    states_the_loss = "NOTHING WAS QUEUED" in message

    # Assert — the sender must not assume the turn is waiting somewhere.
    assert states_the_loss, message


def test_the_refusal_names_the_BUSY_next_step(refusal_message: str) -> None:
    # Arrange
    message = refusal_message

    # Act
    names_the_alternative = "durable rail" in message

    # Assert — busy wants "resend later / use a rail that survives a busy pane".
    assert names_the_alternative, message


def test_the_refusal_names_the_ABSENT_next_step_with_the_agent_name(
    refusal_message: str,
) -> None:
    # Arrange
    message = refusal_message

    # Act — the command must carry the NAME so it is runnable as printed.
    names_the_command = "sac agents start ghost" in message

    # Assert
    assert names_the_command, message


def test_CONTROL_a_DELIVERED_turn_raises_nothing() -> None:
    # Arrange — the same seam, but the pane accepts. A "next step" that also
    # appears on the success path would be noise, not guidance.
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: True)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ok"), runtime=runtime)

    # Act
    result = on_turn("wake up")

    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Launcher — spawn-failure + real SIGTERM teardown
# ---------------------------------------------------------------------------
def test_start_turn_bridge_returns_none_on_spawn_failure(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — spawn raises; the launcher must swallow it and return None.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )

    def raising_spawn(argv, **kwargs):
        raise OSError("exec failed")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="boom", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(
        config, spawn=raising_spawn, port_free_fn=_gate_says_free
    )
    # Assert
    assert pid is None


def test_stop_turn_bridge_sigterms_recorded_pid(isolated_home: Path) -> None:
    # Arrange — a REAL short-lived process whose PID is recorded as the bridge.
    proc = subprocess.Popen(["sleep", "30"])
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="kill-me", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is True
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# start_turn_bridge — pre-existing-bridge teardown (a2a mis-route fix)
# ---------------------------------------------------------------------------
def test_start_turn_bridge_kills_preexisting_bridge_before_spawn(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL prior bridge process recorded in the agent's pidfile
    # (the orphan a port-changing restart would otherwise leave alive), plus
    # a recording spawn for the NEW bridge so no second subprocess is created.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="restart-me", config_path=str(spec)
    )
    prior = subprocess.Popen(["sleep", "30"])
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(prior.pid), encoding="utf-8")
    # Act — start must SIGTERM the prior bridge before spawning the new one.
    bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert — the prior process received SIGTERM and exited.
    assert prior.wait(timeout=5) is not None
    # (defensive: make sure we never leak the helper if the assert above changes)
    if prior.poll() is None:  # pragma: no cover
        prior.kill()


def test_start_turn_bridge_records_new_pid_over_prior(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a stale pidfile pointing at an already-dead PID; the new
    # start must overwrite it with the freshly-spawned bridge's PID (never
    # leave the orphaned/stale value behind).
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="repid-me", config_path=str(spec)
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999", encoding="utf-8")  # dead/stale PID
    # Act
    bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert — pidfile now holds the new bridge's PID.
    assert pid_path.read_text(encoding="utf-8").strip() == str(_PID)
