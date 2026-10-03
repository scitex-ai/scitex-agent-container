"""Private pre-registration proof of a native startup mission's admission.

The launch UUID is already allocated, but the public instance PID is recorded
after runtime.start returns. This observer uses the current pane's captured
kernel birth and its exclusive primary CLI FD; it writes no instance state and
returns no transcript text. Pane echo or an older turn cannot establish proof.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from .._runners._tmux._process_group import _identity
from ._codex_activity import (
    CodexActivityError,
    _read_codex_snapshot,
    _timestamp,
    reduce_codex_activity,
)
from ._codex_activity_binding import assert_codex_binding_current, bind_codex_runtime


def _fresh_mission_turn(lines, *, thread_id, observed_at, issued_at, mission):
    """Require the exact fresh user mission and a fresh accepted primary turn."""
    lines = tuple(lines)
    reduce_codex_activity(lines, expected_thread_id=thread_id, observed_at=observed_at)
    accepted = set()
    messages = []
    for line in lines:
        row = json.loads(line)
        payload = row["payload"]
        if payload.get("thread_id", thread_id) != thread_id:
            continue
        kind = payload.get("type")
        if row.get("type") == "event_msg" and kind == "task_started":
            if _timestamp(row, observed_at) >= issued_at:
                accepted.add(payload["turn_id"])
        elif row.get("type") == "event_msg" and kind == "user_message":
            if payload.get("message") == mission and not payload.get("images"):
                if _timestamp(row, observed_at) >= issued_at:
                    messages.append(payload.get("turn_id"))
        elif (
            row.get("type") == "response_item"
            and kind == "message"
            and payload.get("role") == "user"
        ):
            content = payload.get("content")
            if isinstance(content, list) and all(
                isinstance(item, dict)
                and item.get("type") == "input_text"
                and isinstance(item.get("text"), str)
                for item in content
            ):
                text = "".join(item["text"] for item in content)
                if text == mission and _timestamp(row, observed_at) >= issued_at:
                    messages.append(payload.get("turn_id"))
    return len(accepted) == 1 and any(
        turn is None or turn in accepted for turn in messages
    )


@dataclass(frozen=True)
class StartupAdmissionProbe:
    """One allocated launch, captured pane birth, and exact startup mission."""

    record: Mapping[str, object]
    mission: str
    issued_at: float
    proc_root: Path

    def observed(self, *, observed_at: float) -> bool:
        try:
            binding = bind_codex_runtime(
                self.record,
                instance_id=self.record["id"],
                agent_name=self.record["name"],
                host=self.record["host"],
                proc_root=self.proc_root,
            )
            admitted = _read_codex_snapshot(
                binding.rollout_path,
                expected_file_identity=binding.rollout_identity,
                reduce_fn=lambda lines: _fresh_mission_turn(
                    lines,
                    thread_id=binding.thread_id,
                    observed_at=observed_at,
                    issued_at=self.issued_at,
                    mission=self.mission,
                ),
            )
            assert_codex_binding_current(binding, self.record)
            return admitted
        except CodexActivityError:
            # Missing/incomplete/foreign/replaced source is UNKNOWN, not success.
            return False


def capture_startup_admission(
    config,
    *,
    pane_pid,
    mission,
    proc_root=Path("/proc"),
    issued_at=None,
):
    """Capture only the kernel identity of this newly launched local pane."""
    launch_id = (getattr(config, "env", {}) or {}).get("SAC_INSTANCE_UUID")
    try:
        if not isinstance(launch_id, str) or str(UUID(launch_id)) != launch_id:
            raise ValueError("noncanonical launch UUID")
    except ValueError as error:
        raise CodexActivityError(
            "native bootstrap has no allocated launch UUID"
        ) from error
    if type(pane_pid) is not int or pane_pid <= 0:
        raise CodexActivityError("native bootstrap has no current pane PID")
    pane = _identity(pane_pid, proc_root=proc_root)
    if (
        pane is None
        or pane.state == "Z"
        or pane.uid != os.getuid()
        or not pane.control_group
    ):
        raise CodexActivityError("native bootstrap has no live owned pane birth")
    record = {
        "id": launch_id,
        "name": config.name,
        "host": socket.gethostname(),
        "remote": False,
        "ended_at": None,
        "screen": f"tui-{config.name}",
        "pid": pane.pid,
        "process_start_time": pane.start_time,
        "process_uid": pane.uid,
        "control_group": pane.control_group,
    }
    return StartupAdmissionProbe(
        MappingProxyType(record),
        mission,
        time.time() if issued_at is None else issued_at,
        proc_root,
    )


def wait_for_startup_admission(
    proof,
    *,
    capture_fn,
    timeout_s=30.0,
    poll_s=0.2,
    time_fn=time.monotonic,
    wall_time_fn=time.time,
    sleep_fn=time.sleep,
):
    """Wait boundedly for native admission; a blocking modal sends no keys."""
    from .prompts import codex_blocking_modal

    deadline = time_fn() + timeout_s
    while time_fn() < deadline:
        if codex_blocking_modal(capture_fn()):
            return False
        if proof.observed(observed_at=wall_time_fn()):
            return codex_blocking_modal(capture_fn()) is None
        if poll_s > 0:
            sleep_fn(poll_s)
    return False
