"""An owned live API-runtime fence for preserving its harness at restart."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable

from .._runners._tmux._process_group import _identity
from ..config import AgentConfig


@dataclass(frozen=True)
class SelectedRuntimeFence:
    """Public identity supplied by the owning runtime/route observer.

    ``account`` is a safe registered alias, never a key or account PII. The
    observer must bind the canonical instance to the native session/generation
    and selected route; a pane, historical row, or unknown observation cannot
    provide this record. PID/start/UID identify the canonical stop pane, not an
    unrelated gateway child. The local kernel independently fences PID reuse.
    """

    agent: str
    instance_id: str
    session_id: str
    boot_id: str
    harness: str
    engine: str
    provider: str
    model: str
    account: str
    pid: int
    process_start_time: int
    process_uid: int


def require_selected_stop_target(
    expected: SelectedRuntimeFence, instance: dict | None
) -> None:
    """The routine stop's canonical row must be the observed owned runtime."""
    if not isinstance(expected, SelectedRuntimeFence) or not isinstance(instance, dict):
        raise ValueError("selected-harness-stop-target-unknown")
    if (
        instance.get("id") != expected.instance_id
        or instance.get("name") != expected.agent
        or instance.get("pid") != expected.pid
        or instance.get("process_start_time") != expected.process_start_time
        or instance.get("process_uid") != expected.process_uid
    ):
        raise ValueError("selected-harness-stop-target-mismatch")


def require_selected_runtime(
    config: AgentConfig,
    expected: SelectedRuntimeFence | None,
    observe: Callable[[AgentConfig], SelectedRuntimeFence] | None,
) -> None:
    """Refuse unknown, changed or foreign runtime/route before any stop.

    This seam preserves an already-running Hermes API route. It does not
    change a native subscription harness or select a new provider/model.
    """
    if not isinstance(expected, SelectedRuntimeFence) or observe is None:
        raise ValueError("selected-harness-owned-runtime-required")
    labels = (
        expected.instance_id,
        expected.session_id,
        expected.boot_id,
        expected.engine,
        expected.provider,
        expected.model,
    )
    if (
        config.harness != "hermes"
        or expected.harness != "hermes"
        or config.subscription_provider
        or any(not isinstance(value, str) or not value.strip() for value in labels)
        or not isinstance(expected.account, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expected.account)
        or type(expected.pid) is not int
        or expected.pid <= 0
        or type(expected.process_start_time) is not int
        or expected.process_start_time <= 0
        or type(expected.process_uid) is not int
        or expected.process_uid != os.getuid()
    ):
        raise ValueError("selected-harness-runtime-refused")
    from ._api_salvage import _selected_identity

    if _selected_identity(config) != (
        expected.agent,
        expected.harness,
        expected.engine,
        expected.provider,
        expected.model,
    ):
        raise ValueError("selected-harness-model-route-mismatch")
    before = _identity(expected.pid)
    if (
        before is None
        or before.state == "Z"
        or before.start_time != expected.process_start_time
        or before.uid != expected.process_uid
    ):
        raise ValueError("selected-harness-process-incarnation-changed")
    try:
        observed = observe(config)
    except Exception:
        raise ValueError("selected-harness-runtime-unobservable") from None
    after = _identity(expected.pid)
    if (
        observed != expected
        or after is None
        or after.state == "Z"
        or (
            after.pid,
            after.start_time,
            after.uid,
            after.process_group,
            after.session,
            after.control_group,
        )
        != (
            before.pid,
            before.start_time,
            before.uid,
            before.process_group,
            before.session,
            before.control_group,
        )
    ):
        raise ValueError("selected-harness-runtime-fence-changed")
