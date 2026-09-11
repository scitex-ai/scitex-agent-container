from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.runtimes._hermes_autonomous_wakeup import (
    _stable_stagger_seconds,
    arm_hermes_autonomous_wakeup,
    hermes_heartbeat_plan,
)


def _config(**autonomous):
    values = {
        "enabled": True,
        "idle_kick_after_s": 120,
        "kick_text": "Continue autonomously.",
        **autonomous,
    }
    return SimpleNamespace(
        name="hub",
        harness="hermes",
        runtime="tui",
        autonomous=SimpleNamespace(**values),
    )


class _Runtime:
    def __init__(self, accepted: bool = True, idle: bool = True):
        self.accepted = accepted
        self.idle = idle
        self.calls = []

    def autonomous_control_is_idle(self, config):
        self.calls.append((config.name, "observe-idle", None))
        return self.idle

    def send_turn(self, config, text, *, wait_ready=True):
        self.calls.append((config.name, text, wait_ready))
        return self.accepted


def test_plan_uses_configured_idle_backoff_and_cards_ownership_contract():
    # Arrange
    config = _config()
    # Act
    plan = hermes_heartbeat_plan(config)
    # Assert
    assert (
        plan is not None,
        plan.configured_interval_seconds if plan else None,
        "durable Cards inbox/board" in plan.prompt if plan else False,
        "verify its assignee and ownership" in plan.prompt if plan else False,
        "does not overlap active work" in plan.prompt if plan else False,
        plan.prompt.endswith("Continue autonomously.") if plan else False,
    ) == (True, 120, True, True, True, True)


def test_plan_applies_hermes_busy_loop_floor():
    # Arrange
    config = _config(idle_kick_after_s=5)
    # Act
    plan = hermes_heartbeat_plan(config)
    # Assert
    assert (
        plan is not None,
        plan.configured_interval_seconds if plan else None,
        plan.interval_seconds >= 60 if plan else False,
    ) == (True, 60, True)


def test_plan_collapses_multiline_kick_for_one_slash_command():
    # Arrange
    config = _config(kick_text="Continue.\nDo not stop.")
    # Act
    plan = hermes_heartbeat_plan(config)
    # Assert
    assert (
        plan is not None,
        "Continue. Do not stop." in plan.command if plan else False,
        "\n" not in plan.command if plan else False,
    ) == (True, True, True)


def test_non_hermes_and_disabled_specs_do_not_arm():
    # Arrange
    disabled = _config()
    disabled.autonomous.enabled = False
    claude = _config()
    claude.harness = "claude-code"
    # Act
    plans = hermes_heartbeat_plan(disabled), hermes_heartbeat_plan(claude)
    # Assert
    assert plans == (None, None)


def test_arm_submits_native_command_only_after_positive_idle_observation():
    # Arrange
    runtime = _Runtime()
    config = _config()
    # Act
    armed = arm_hermes_autonomous_wakeup(runtime, config)
    # Assert
    assert (
        armed,
        runtime.calls[0],
        runtime.calls[1][1].startswith("/heartbeat every 177s "),
        runtime.calls[1][2],
    ) == (True, ("hub", "observe-idle", None), True, False)


def test_arm_does_not_paste_while_initial_turn_is_busy():
    # Arrange
    runtime = _Runtime(idle=False)
    config = _config()
    # Act
    armed = arm_hermes_autonomous_wakeup(runtime, config)
    # Assert
    assert (armed, runtime.calls) == (
        False,
        [("hub", "observe-idle", None)],
    )


def test_arm_reports_refusal_without_retrying_or_burning_turns():
    # Arrange
    runtime = _Runtime(accepted=False)
    config = _config()
    # Act
    armed = arm_hermes_autonomous_wakeup(runtime, config)
    # Assert
    assert (armed, len(runtime.calls)) == (False, 2)


def test_plan_defers_external_ci_polling_to_a_later_heartbeat():
    # Arrange
    config = _config()
    # Act
    plan = hermes_heartbeat_plan(config)
    # Assert
    assert plan is not None and all(
        text in plan.prompt
        for text in (
            "Do not keep this turn active with sleep commands solely to poll external CI",
            "let a later heartbeat recheck",
            "real test, build, or useful process that is already running",
        )
    )


def test_stagger_is_deterministic_and_bounded_by_one_minute():
    # Arrange
    names = ("scitex-hub", "scitex-app", "scitex-figrecipe")
    # Act
    first = tuple(_stable_stagger_seconds(name, 120) for name in names)
    second = tuple(_stable_stagger_seconds(name, 120) for name in names)
    # Assert
    assert (first, second, all(0 <= value < 60 for value in first)) == (
        (7, 21, 30),
        (7, 21, 30),
        True,
    )


def test_stagger_window_never_exceeds_configured_interval():
    # Arrange
    configured_interval = 17
    # Act
    stagger = _stable_stagger_seconds("hub", configured_interval)
    # Assert
    assert 0 <= stagger < configured_interval
