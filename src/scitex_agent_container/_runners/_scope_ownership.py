"""Identity-safe ownership of one systemd scope-backed runtime."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ._tmux._process_group import ProcessIdentity, _identity

_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
_DEFAULT_SCOPE_STOP_TIMEOUT_S = 95.0


@dataclass(frozen=True)
class ScopeOwnership:
    """Kernel and systemd identity of the scope that owns one incarnation."""

    pid: int
    process_start_time: int
    process_uid: int
    control_group: str
    scope_unit: str
    scope_invocation_id: str

    def to_record_fields(self) -> dict[str, int | str]:
        return {
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "process_uid": self.process_uid,
            "control_group": self.control_group,
            "scope_unit": self.scope_unit,
            "scope_invocation_id": self.scope_invocation_id,
        }


def _show_scope(unit: str) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ControlGroup",
                "--property=InvocationID",
                "--property=ActiveState",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return fields


def capture_scope_ownership(
    pid: int,
    *,
    identity_fn: Callable[[int], ProcessIdentity | None] = _identity,
    show_scope_fn: Callable[[str], dict[str, str] | None] = _show_scope,
) -> ScopeOwnership | None:
    """Capture the exact dedicated scope containing ``pid``, or ``None``.

    The tuple is deliberately redundant: PID + kernel start time defeats PID
    reuse, while cgroup + systemd InvocationID defeats transient unit-name
    reuse.  A later stop must match every component before signalling.
    """
    identity = identity_fn(pid)
    if identity is None or identity.uid != os.getuid():
        return None
    control_group = identity.control_group
    unit = PurePosixPath(control_group).name
    if not control_group or control_group == "/" or not unit.endswith(".scope"):
        return None
    shown = show_scope_fn(unit)
    if shown is None or shown.get("ControlGroup") != control_group:
        return None
    invocation = shown.get("InvocationID", "").lower()
    if _INVOCATION_ID.fullmatch(invocation) is None:
        return None
    return ScopeOwnership(
        pid=identity.pid,
        process_start_time=identity.start_time,
        process_uid=identity.uid,
        control_group=control_group,
        scope_unit=unit,
        scope_invocation_id=invocation,
    )


def ownership_from_record(record: Mapping[str, Any]) -> ScopeOwnership | None:
    """Rebuild ownership only when every launch-recorded field is present."""
    try:
        ownership = ScopeOwnership(
            pid=int(record["pid"]),
            process_start_time=int(record["process_start_time"]),
            process_uid=int(record["process_uid"]),
            control_group=str(record["control_group"]),
            scope_unit=str(record["scope_unit"]),
            scope_invocation_id=str(record["scope_invocation_id"]).lower(),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if (
        ownership.pid <= 0
        or ownership.process_uid != os.getuid()
        or ownership.control_group == "/"
        or PurePosixPath(ownership.control_group).name != ownership.scope_unit
        or not ownership.scope_unit.endswith(".scope")
        or _INVOCATION_ID.fullmatch(ownership.scope_invocation_id) is None
    ):
        return None
    return ownership


def _same_process(
    ownership: ScopeOwnership,
    identity_fn: Callable[[int], ProcessIdentity | None],
) -> bool:
    current = identity_fn(ownership.pid)
    return bool(
        current is not None
        and current.state != "Z"
        and current.start_time == ownership.process_start_time
        and current.uid == ownership.process_uid
        and current.control_group == ownership.control_group
    )


def _cgroup_pids(control_group: str) -> tuple[int, ...] | None:
    root = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if not root.exists():
        return ()
    try:
        pids = {
            int(raw)
            for procs in root.rglob("cgroup.procs")
            for raw in procs.read_text().split()
        }
    except (OSError, ValueError):
        return None
    return tuple(sorted(pids))


def _stop_scope(
    unit: str,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    try:
        result = run_fn(
            ["systemctl", "--user", "stop", "--no-block", unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_owned_scope_down(
    record: Mapping[str, Any],
    *,
    identity_fn: Callable[[int], ProcessIdentity | None] = _identity,
    show_scope_fn: Callable[[str], dict[str, str] | None] = _show_scope,
    cgroup_pids_fn: Callable[[str], tuple[int, ...] | None] = _cgroup_pids,
    stop_scope_fn: Callable[[str], bool] = _stop_scope,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    timeout_s: float = _DEFAULT_SCOPE_STOP_TIMEOUT_S,
) -> bool:
    """Stop and verify exactly the scope recorded for one incarnation.

    Returns true only after both independent observations are terminal: the
    launch PID identity is gone and the recorded cgroup has no processes.
    Nothing is signalled until the live systemd unit matches both its recorded
    ControlGroup and InvocationID.  The stop request is non-blocking because
    systemd may spend up to its default 90-second ``TimeoutStopSec`` draining
    a scope; this function owns the identity-safe observation window instead
    of timing out the ``systemctl`` client while the stop job keeps running.
    """
    ownership = ownership_from_record(record)
    if ownership is None:
        return False
    pids = cgroup_pids_fn(ownership.control_group)
    same_process = _same_process(ownership, identity_fn)
    if pids == () and not same_process:
        return True
    if pids is None:
        return False
    shown = show_scope_fn(ownership.scope_unit)
    if shown is None:
        return False
    if (
        shown.get("ControlGroup") != ownership.control_group
        or shown.get("InvocationID", "").lower() != ownership.scope_invocation_id
    ):
        return False
    if any(
        (identity := identity_fn(pid)) is None
        or identity.uid != ownership.process_uid
        or identity.control_group != ownership.control_group
        for pid in pids
    ):
        return False
    if not stop_scope_fn(ownership.scope_unit):
        return False
    deadline = monotonic_fn() + timeout_s
    while True:
        pids = cgroup_pids_fn(ownership.control_group)
        if pids == () and not _same_process(ownership, identity_fn):
            return True
        if pids is None or monotonic_fn() >= deadline:
            return False
        sleep_fn(0.05)


__all__ = [
    "ScopeOwnership",
    "capture_scope_ownership",
    "ensure_owned_scope_down",
    "ownership_from_record",
]
