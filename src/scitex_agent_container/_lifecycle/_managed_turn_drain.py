"""Fail-closed turn guard for managed Hermes teardown."""

from __future__ import annotations

import scitex_logging as slogging
import math
import time
from dataclasses import dataclass
from typing import Callable

from ..config import AgentConfig
from ..runtimes._hermes_tui_rpc import HermesTurnActivity, observe_turn_activity
from ..runtimes.tui_session import state_dir_for_config

logger = slogging.getLogger(__name__)


class ManagedTurnDrainRefusal(RuntimeError):
    """A normal stop would destroy a live or unobservable Hermes turn."""


@dataclass(frozen=True)
class TurnDrainResult:
    status: str
    waited_seconds: float
    forced: bool = False


def _default_probe(config: AgentConfig) -> HermesTurnActivity:
    return observe_turn_activity(state_dir_for_config(config), config.name)


def guard_managed_turn(
    config: AgentConfig,
    *,
    allow_active_turn_kill: bool,
    timeout_s: float = 0.0,
    poll_s: float = 0.5,
    probe: Callable[[AgentConfig], HermesTurnActivity] = _default_probe,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TurnDrainResult | None:
    """Wait for Hermes to become idle or refuse before runtime teardown.

    Other harnesses return ``None`` because SAC has no equivalent authoritative
    live-turn API for them yet.  An unavailable Hermes observation fails closed:
    killing when the gateway cannot answer would turn uncertainty into data and
    prefix-cache loss.
    """
    if str(getattr(config, "harness", "") or "").lower() != "hermes":
        return None
    timeout_s = float(timeout_s)
    if not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("timeout_s must be a finite non-negative number")
    began = monotonic_fn()

    if allow_active_turn_kill:
        logger.warning(
            "FORCED Hermes teardown for %s bypasses live-turn drain; the active "
            "response and its SGLang prefix-cache residency may be lost",
            config.name,
        )
        return TurnDrainResult(status="force-bypassed", waited_seconds=0.0, forced=True)

    deadline = began + timeout_s
    while True:
        try:
            activity = probe(config)
        except Exception as exc:
            raise ManagedTurnDrainRefusal(
                f"Refusing to stop Hermes agent {config.name!r}: its native "
                f"session activity is unavailable ({exc}). Detach safely with "
                f"`tmux detach`; retry when `sac agents status {config.name}` can "
                "observe Hermes, or use `--force` only if losing the active turn "
                "and SGLang prefix cache is acceptable."
            ) from exc
        if activity.idle:
            return TurnDrainResult(
                status=activity.session_status,
                waited_seconds=max(0.0, monotonic_fn() - began),
            )
        now = monotonic_fn()
        if now >= deadline:
            waited = max(0.0, now - began)
            raise ManagedTurnDrainRefusal(
                f"Refusing to stop Hermes agent {config.name!r}: session "
                f"{activity.session_id!r} is {activity.session_status!r} after "
                f"waiting {waited:.1f}s. Let the turn finish, or retry with "
                f"`--drain-timeout SECONDS`; `--force` may lose the active "
                "response and SGLang prefix cache. Tmux detach is always safe."
            )
        sleep_fn(min(poll_s, max(0.0, deadline - now)))


__all__ = [
    "ManagedTurnDrainRefusal",
    "TurnDrainResult",
    "guard_managed_turn",
]
