from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.runtimes._hermes_autonomous_wakeup import (
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
    def __init__(self, accepted: bool = True):
        self.accepted = accepted
        self.calls = []

    def send_turn(self, config, text, *, wait_ready=True):
        self.calls.append((config.name, text, wait_ready))
        return self.accepted


def test_plan_uses_configured_idle_backoff_and_cards_ownership_contract():
    plan = hermes_heartbeat_plan(_config())

    assert (
        plan is not None,
        plan.interval_seconds if plan else None,
        "durable Cards inbox/board" in plan.prompt if plan else False,
        "verify its assignee and ownership" in plan.prompt if plan else False,
        "does not overlap active work" in plan.prompt if plan else False,
        plan.prompt.endswith("Continue autonomously.") if plan else False,
    ) == (True, 120, True, True, True, True)


def test_plan_applies_hermes_busy_loop_floor():
    plan = hermes_heartbeat_plan(_config(idle_kick_after_s=5))

    assert (plan is not None, plan.interval_seconds if plan else None) == (True, 60)


def test_plan_collapses_multiline_kick_for_one_slash_command():
    plan = hermes_heartbeat_plan(_config(kick_text="Continue.\nDo not stop."))

    assert (
        plan is not None,
        "Continue. Do not stop." in plan.command if plan else False,
        "\n" not in plan.command if plan else False,
    ) == (True, True, True)


def test_non_hermes_and_disabled_specs_do_not_arm():
    disabled = _config()
    disabled.autonomous.enabled = False
    claude = _config()
    claude.harness = "claude-code"

    assert (hermes_heartbeat_plan(disabled), hermes_heartbeat_plan(claude)) == (
        None,
        None,
    )


def test_arm_submits_native_command_without_waiting_for_ready():
    runtime = _Runtime()

    armed = arm_hermes_autonomous_wakeup(runtime, _config())

    assert (
        armed,
        runtime.calls[0][0],
        runtime.calls[0][1].startswith("/heartbeat every 120s "),
        runtime.calls[0][2],
    ) == (True, "hub", True, False)


def test_arm_reports_refusal_without_retrying_or_burning_turns():
    runtime = _Runtime(accepted=False)

    armed = arm_hermes_autonomous_wakeup(runtime, _config())

    assert (armed, len(runtime.calls)) == (False, 1)
