"""Arm Hermes' native idle heartbeat for autonomous Cards work.

SAC owns the declarative switch and the deployment boundary.  Hermes owns the
actual scheduler: its session heartbeat fires only while the session is idle
and its input queue is empty, so human input/steering always wins and a busy
model does not receive repeated continuation turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_HERMES_MIN_HEARTBEAT_SECONDS = 60

_CARDS_WORK_CONTRACT = (
    "Check the durable Cards inbox/board for useful work in your scope. Before "
    "editing, re-read the live card and verify its assignee and ownership. Continue "
    "work already owned by you first; otherwise claim exactly one eligible unowned "
    "card only after checking that its file/scope does not overlap active work owned "
    "by another agent. Never edit another agent's claimed scope. If Cards is "
    "unavailable or no safe work exists, report that briefly and remain idle."
)


@dataclass(frozen=True)
class HermesHeartbeatPlan:
    """One idempotent native-heartbeat command for an owning Hermes TUI."""

    interval_seconds: int
    prompt: str

    @property
    def command(self) -> str:
        return f"/heartbeat every {self.interval_seconds}s {self.prompt}"


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def hermes_heartbeat_plan(config: Any) -> HermesHeartbeatPlan | None:
    """Compile ``spec.autonomous`` into a Hermes-native idle wakeup.

    Hermes enforces a 60-second minimum because a shorter recurrence is a busy
    loop.  SAC preserves that safety floor while retaining the authored value
    above it as the configurable idle backoff.
    """
    harness = str(getattr(config, "harness", "") or "").strip().lower()
    runtime = str(getattr(config, "runtime", "") or "").strip().lower()
    autonomous = getattr(config, "autonomous", None)
    if (
        harness != "hermes"
        or runtime != "tui"
        or not getattr(autonomous, "enabled", False)
    ):
        return None

    authored_interval = int(getattr(autonomous, "idle_kick_after_s", 120) or 120)
    interval = max(_HERMES_MIN_HEARTBEAT_SECONDS, authored_interval)
    kick = _one_line(getattr(autonomous, "kick_text", ""))
    prompt = f"{_CARDS_WORK_CONTRACT} {kick}" if kick else _CARDS_WORK_CONTRACT
    return HermesHeartbeatPlan(interval_seconds=interval, prompt=prompt)


def arm_hermes_autonomous_wakeup(runtime: Any, config: Any) -> bool | None:
    """Submit the native slash command once; Hermes schedules later wakeups.

    ``None`` means the spec does not request this feature.  Hermes treats slash
    commands as local control input even while a model turn is running, so this
    does not interrupt or enqueue ahead of a human turn.
    """
    plan = hermes_heartbeat_plan(config)
    if plan is None:
        return None
    return bool(runtime.send_turn(config, plan.command, wait_ready=False))


__all__ = [
    "HermesHeartbeatPlan",
    "arm_hermes_autonomous_wakeup",
    "hermes_heartbeat_plan",
]
