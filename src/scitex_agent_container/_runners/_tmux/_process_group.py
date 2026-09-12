"""Identity-safe termination of the process group owned by one tmux pane."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent_pid: int
    process_group: int
    session: int
    start_time: int
    uid: int
    state: str
    control_group: str


def _control_group(pid: int) -> str:
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers:
            return path
    return ""


def _identity(pid: int) -> ProcessIdentity | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        close = stat.rfind(")")
        fields = stat[close + 2 :].split()
        return ProcessIdentity(
            pid=pid,
            parent_pid=int(fields[1]),
            process_group=int(fields[2]),
            session=int(fields[3]),
            start_time=int(fields[19]),
            uid=Path(f"/proc/{pid}").stat().st_uid,
            state=fields[0],
            control_group=_control_group(pid),
        )
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def capture_owned_process_group(pane_pid: int) -> tuple[ProcessIdentity, ...]:
    """Snapshot the pane group, refusing unsafe or cross-user identities."""
    pane = _identity(pane_pid)
    if (
        pane is None
        or pane.uid != os.getuid()
        or pane.process_group <= 1
        or pane.process_group == os.getpgrp()
    ):
        return ()
    members: list[ProcessIdentity] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        member = _identity(int(entry.name))
        if member is None or member.process_group != pane.process_group:
            continue
        if member.uid != pane.uid or member.session != pane.session:
            return ()
        members.append(member)
    return tuple(sorted(members, key=lambda item: item.pid))


def capture_owned_process_tree(pane_pid: int) -> tuple[ProcessIdentity, ...]:
    """Snapshot every same-user descendant of the pane before teardown."""
    pane = _identity(pane_pid)
    if pane is None or pane.uid != os.getuid():
        return ()
    processes = tuple(
        identity
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit()
        if (identity := _identity(int(entry.name))) is not None
        and identity.uid == pane.uid
    )
    owned = {pane.pid}
    changed = True
    while changed:
        before = len(owned)
        owned.update(item.pid for item in processes if item.parent_pid in owned)
        changed = len(owned) != before
    snapshot = tuple(
        sorted(
            (item for item in processes if item.pid in owned), key=lambda item: item.pid
        )
    )
    if any(
        item.process_group <= 1 or item.process_group == os.getpgrp()
        for item in snapshot
    ):
        return ()
    return snapshot


def _same_process(identity: ProcessIdentity) -> bool:
    current = _identity(identity.pid)
    return (
        current is not None
        and current.start_time == identity.start_time
        and current.process_group == identity.process_group
        and current.session == identity.session
        and current.uid == identity.uid
        and current.control_group == identity.control_group
        and current.state != "Z"
    )


def _survivors(snapshot: tuple[ProcessIdentity, ...]) -> tuple[ProcessIdentity, ...]:
    return tuple(identity for identity in snapshot if _same_process(identity))


def _group_still_safe(snapshot: tuple[ProcessIdentity, ...]) -> bool:
    """Refuse a group-wide signal if an uncaptured process joined the group."""
    if not snapshot:
        return False
    current = capture_owned_process_group(snapshot[0].pid)
    if not current:
        # The pane leader may be gone. Inspect the group through a survivor.
        survivor = next(iter(_survivors(snapshot)), None)
        current = capture_owned_process_group(survivor.pid) if survivor else ()
    captured = {(item.pid, item.start_time) for item in snapshot}
    return bool(current) and all(
        (item.pid, item.start_time) in captured for item in current
    )


def _group_still_owned(
    snapshot: tuple[ProcessIdentity, ...], control_group: str
) -> bool:
    """Verify every current PGID member remains inside the owned cgroup."""
    if not snapshot:
        return False
    representative = next(iter(_survivors(snapshot)), None)
    if representative is None:
        return True
    current = capture_owned_process_group(representative.pid)
    return bool(current) and all(
        item.control_group == control_group for item in current
    )


def _wait_for_group_death(
    snapshot: tuple[ProcessIdentity, ...],
    *,
    timeout_s: float,
    sleep_fn: Callable[[float], None],
) -> bool:
    deadline = time.monotonic() + timeout_s
    while _survivors(snapshot):
        if time.monotonic() >= deadline:
            return False
        sleep_fn(0.05)
    return True


def terminate_owned_process_group(
    snapshot: tuple[ProcessIdentity, ...],
    *,
    term_timeout_s: float = 2.0,
    kill_timeout_s: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    killpg_fn: Callable[[int, int], None] = os.killpg,
) -> bool:
    """TERM, then bounded KILL, returning true only after identity death."""
    if not snapshot or not _group_still_safe(snapshot):
        return False
    pgid = snapshot[0].process_group
    try:
        killpg_fn(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return not _survivors(snapshot)
    except (PermissionError, OSError):
        return False
    if _wait_for_group_death(snapshot, timeout_s=term_timeout_s, sleep_fn=sleep_fn):
        return True
    if not _group_still_safe(snapshot):
        return False
    try:
        killpg_fn(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return not _survivors(snapshot)
    except (PermissionError, OSError):
        return False
    return _wait_for_group_death(snapshot, timeout_s=kill_timeout_s, sleep_fn=sleep_fn)


def terminate_owned_process_tree(
    snapshot: tuple[ProcessIdentity, ...],
    *,
    term_timeout_s: float = 2.0,
    kill_timeout_s: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    killpg_fn: Callable[[int, int], None] = os.killpg,
    cgroup_empty_fn: Callable[[str], bool] | None = None,
    cgroup_kill_fn: Callable[[str], bool] | None = None,
) -> bool:
    """Terminate every captured group and verify the owning cgroup is empty.

    The cgroup check closes the fork-after-snapshot race: a new child can have
    a PID and process group absent from the userspace snapshot, but it cannot
    leave the pane's dedicated systemd scope merely by forking.  On a platform
    without a dedicated pane cgroup, only a single process group can be
    verified; a multi-group tree receives a conservative ``False`` verdict.
    """
    if not snapshot:
        return False
    is_cgroup_empty = cgroup_empty_fn or _control_group_empty
    kill_cgroup = cgroup_kill_fn or _kill_control_group
    control_group = snapshot[0].control_group
    has_dedicated_cgroup = not (
        not control_group
        or control_group == _control_group(os.getpid())
        or control_group == "/"
        or any(item.control_group != control_group for item in snapshot)
    )
    if not has_dedicated_cgroup:
        groups = {item.process_group for item in snapshot}
        if len(groups) != 1:
            return False
        return terminate_owned_process_group(
            snapshot,
            term_timeout_s=term_timeout_s,
            kill_timeout_s=kill_timeout_s,
            sleep_fn=sleep_fn,
            killpg_fn=killpg_fn,
        )
    groups = tuple(sorted({item.process_group for item in snapshot}, reverse=True))
    for pgid in groups:
        members = tuple(item for item in snapshot if item.process_group == pgid)
        # Forks after the snapshot are allowed only while they remain inside
        # the dedicated scope.  This keeps TERM safe without mistaking a
        # newly forked owned child for an unrelated process.
        if not _group_still_owned(members, control_group):
            return False
        try:
            killpg_fn(pgid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError):
            return False
    _wait_for_group_death(snapshot, timeout_s=term_timeout_s, sleep_fn=sleep_fn)
    if is_cgroup_empty(control_group):
        return True
    # Atomic, recursive kernel boundary: this also catches a child forked
    # after the snapshot and a descendant that created another session/PGID.
    if not kill_cgroup(control_group):
        return False
    deadline = time.monotonic() + kill_timeout_s
    while not is_cgroup_empty(control_group):
        if time.monotonic() >= deadline:
            return False
        sleep_fn(0.05)
    return True


def _kill_control_group(control_group: str) -> bool:
    root = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    try:
        (root / "cgroup.kill").write_text("1")
    except OSError:
        return False
    return True


def _control_group_empty(control_group: str) -> bool:
    root = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    try:
        for procs in root.rglob("cgroup.procs"):
            if procs.read_text().strip():
                return False
    except OSError:
        return False
    return True


__all__ = [
    "ProcessIdentity",
    "capture_owned_process_group",
    "capture_owned_process_tree",
    "terminate_owned_process_group",
    "terminate_owned_process_tree",
]
