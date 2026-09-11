"""Arm Hermes' native idle heartbeat for autonomous Cards work.

SAC owns the declarative switch and the deployment boundary.  Hermes owns the
actual scheduler: its session heartbeat fires only while the session is idle
and its input queue is empty, so human input/steering always wins and a busy
model does not receive repeated continuation turns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

_HERMES_MIN_HEARTBEAT_SECONDS = 60

_CARDS_WORK_CONTRACT = (
    "Check the durable Cards inbox/board for useful work in your scope. Before "
    "editing, re-read the live card and verify its assignee and ownership. Continue "
    "work already owned by you first; otherwise claim exactly one eligible unowned "
    "card only after checking that its file/scope does not overlap active work owned "
    "by another agent. Never edit another agent's claimed scope. If Cards is "
    "unavailable or no safe work exists, report that briefly and remain idle. Do "
    "not keep this turn active with sleep commands solely to poll external CI, "
    "review, or status: record the pending evidence, end the turn, and let a later "
    "heartbeat recheck. This does not apply to a real test, build, or useful process "
    "that is already running; monitor legitimate work normally."
)


@dataclass(frozen=True)
class HermesHeartbeatPlan:
    """One idempotent native-heartbeat command for an owning Hermes TUI."""

    configured_interval_seconds: int
    stagger_seconds: int
    prompt: str

    @property
    def interval_seconds(self) -> int:
        """Effective cadence: configured lower bound plus stable staggering."""
        return self.configured_interval_seconds + self.stagger_seconds

    @property
    def command(self) -> str:
        return f"/heartbeat every {self.interval_seconds}s {self.prompt}"


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _stable_stagger_seconds(agent_name: object, interval_seconds: int) -> int:
    """Return a cross-process-stable phase spread without random state.

    Python's built-in ``hash`` is salted per process, so using it would move an
    agent on every launch.  The first eight SHA-256 bytes provide a stable
    integer.  The window never exceeds one minute or the configured interval.
    """
    window = min(60, interval_seconds)
    digest = hashlib.sha256(str(agent_name or "").encode()).digest()
    return int.from_bytes(digest[:8], "big") % window


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
    stagger = _stable_stagger_seconds(getattr(config, "name", ""), interval)
    kick = _one_line(getattr(autonomous, "kick_text", ""))
    prompt = f"{_CARDS_WORK_CONTRACT} {kick}" if kick else _CARDS_WORK_CONTRACT
    return HermesHeartbeatPlan(
        configured_interval_seconds=interval,
        stagger_seconds=stagger,
        prompt=prompt,
    )


def arm_hermes_autonomous_wakeup(runtime: Any, config: Any) -> bool | None:
    """Submit the native slash command only from an observed idle composer.

    ``None`` means the spec does not request this feature.  Hermes treats slash
    commands as local control input, but text pasted during a live model turn
    remains visible in the shared composer and prevents immediate human input.
    The detached Hermes monitor retries this observation on later ticks; a busy
    result therefore means "not yet", never "paste now and hope".
    """
    plan = hermes_heartbeat_plan(config)
    if plan is None:
        return None
    if not runtime.autonomous_control_is_idle(config):
        return False
    return bool(runtime.send_turn(config, plan.command, wait_ready=False))


__all__ = [
    "HermesHeartbeatPlan",
    "arm_hermes_autonomous_wakeup",
    "hermes_heartbeat_plan",
]
