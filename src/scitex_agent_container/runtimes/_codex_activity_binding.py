"""Bind native activity to a canonical instance and its owned CLI root FD."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import UUID

from .._runners._tmux._process_group import (
    ProcessIdentity,
    _identity,
    capture_owned_process_tree,
)
from ._codex_activity import CodexActivityError

MAX_ROLLOUT_HEADER_BYTES = 1024 * 1024
MAX_OWNER_FDS = 1024


@dataclass(frozen=True)
class NativeCodexBinding:
    instance_id: str
    agent_name: str
    host: str
    pane: ProcessIdentity
    native: ProcessIdentity
    thread_id: str
    rollout_fd: int
    rollout_identity: tuple[int, int]
    proc_root: Path

    @property
    def rollout_path(self) -> Path:
        # Opening the FD reference follows the owner's mount namespace without
        # borrowing the observer's HOME or translating a private bind by guess.
        return self.proc_root / str(self.native.pid) / "fd" / str(self.rollout_fd)

    @property
    def boot_id(self) -> str:
        return f"{self.instance_id}:{self.native.pid}:{self.native.start_time}"


def _stable_identity(identity: ProcessIdentity) -> tuple:
    return (
        identity.pid,
        identity.parent_pid,
        identity.process_group,
        identity.session,
        identity.start_time,
        identity.uid,
        identity.control_group,
    )


def _assert_instance(
    record: Mapping,
    *,
    instance_id: str,
    agent_name: str,
    host: str,
    proc_root: Path,
) -> ProcessIdentity:
    if (
        record.get("id") != instance_id
        or record.get("name") != agent_name
        or record.get("host") != host
        or record.get("ended_at") is not None
        or record.get("remote") not in {False, 0}
        or record.get("screen") != f"tui-{agent_name}"
    ):
        raise CodexActivityError(
            "native owner does not match the active local instance"
        )
    for field in ("pid", "process_start_time", "process_uid"):
        if type(record.get(field)) is not int or record[field] < 0:
            raise CodexActivityError("native instance lacks kernel ownership identity")
    pane = _identity(record["pid"], proc_root=proc_root)
    if (
        pane is None
        or pane.state == "Z"
        or pane.uid != os.getuid()
        or pane.start_time != record["process_start_time"]
        or pane.uid != record["process_uid"]
        or not record.get("control_group")
        or pane.control_group != record["control_group"]
    ):
        raise CodexActivityError("native instance kernel ownership changed")
    return pane


def _cli_root_header(path: Path) -> tuple[str, tuple[int, int]] | None:
    identity = path.stat()
    if not stat.S_ISREG(identity.st_mode):
        return None
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
            raise CodexActivityError("native rollout FD changed before identity read")
        line = stream.readline(MAX_ROLLOUT_HEADER_BYTES + 1)
    if len(line) > MAX_ROLLOUT_HEADER_BYTES or not line.endswith(b"\n"):
        raise CodexActivityError("native rollout has an incomplete identity header")
    try:
        row = json.loads(line)
        payload = row["payload"]
        if row.get("type") != "session_meta" or not isinstance(payload, dict):
            raise ValueError("missing primary session metadata")
        source = payload.get("source")
        if source != "cli":
            # Child FDs are deliberately opened by the same native process.
            # Their activity is owned by another thread, even inside this tree.
            return None
        thread_id = str(UUID(payload["id"]))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise CodexActivityError(
            "native rollout has malformed primary identity"
        ) from exc
    current = path.stat()
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise CodexActivityError("native rollout FD changed during identity read")
    return thread_id, (opened.st_dev, opened.st_ino)


def bind_codex_runtime(
    record: Mapping,
    *,
    instance_id: str,
    agent_name: str,
    host: str,
    proc_root: Path = Path("/proc"),
) -> NativeCodexBinding:
    """Find exactly one CLI root already open in the canonical owned tree."""
    pane = _assert_instance(
        record,
        instance_id=instance_id,
        agent_name=agent_name,
        host=host,
        proc_root=proc_root,
    )
    roots: dict[tuple[int, int], NativeCodexBinding] = {}
    try:
        tree = capture_owned_process_tree(pane.pid, proc_root=proc_root)
        for native in tree:
            if native.state == "Z" or native.control_group != pane.control_group:
                continue
            process = proc_root / str(native.pid)
            if (process / "comm").read_text().strip() != "codex":
                continue
            fds = list((process / "fd").iterdir())
            if len(fds) > MAX_OWNER_FDS:
                raise CodexActivityError("native owner exceeds bounded FD inspection")
            for fd in sorted(fds, key=lambda item: item.name):
                if not fd.name.isdigit():
                    continue
                target = fd.readlink()
                if not (
                    target.name.startswith("rollout-")
                    and target.name.endswith(".jsonl")
                ):
                    continue
                header = _cli_root_header(fd)
                if header is None:
                    continue
                thread_id, file_identity = header
                binding = NativeCodexBinding(
                    instance_id,
                    agent_name,
                    host,
                    pane,
                    native,
                    thread_id,
                    int(fd.name),
                    file_identity,
                    proc_root,
                )
                previous = roots.get(file_identity)
                if previous is not None and previous.native.pid != native.pid:
                    raise CodexActivityError(
                        "native rollout has multiple process owners"
                    )
                roots[file_identity] = binding
    except (OSError, ValueError) as exc:
        if isinstance(exc, CodexActivityError):
            raise
        raise CodexActivityError(
            "native owned process or FD inspection is unavailable"
        ) from exc
    if len(roots) != 1:
        raise CodexActivityError("native owned tree has no exclusive CLI root rollout")
    binding = next(iter(roots.values()))
    assert_codex_binding_current(binding, record)
    return binding


def assert_codex_binding_current(binding: NativeCodexBinding, record: Mapping) -> None:
    """Fence process reuse, changed lineage and FD reuse before publication."""
    pane = _assert_instance(
        record,
        instance_id=binding.instance_id,
        agent_name=binding.agent_name,
        host=binding.host,
        proc_root=binding.proc_root,
    )
    native = _identity(binding.native.pid, proc_root=binding.proc_root)
    if (
        _stable_identity(pane) != _stable_identity(binding.pane)
        or native is None
        or native.state == "Z"
        or _stable_identity(native) != _stable_identity(binding.native)
    ):
        raise CodexActivityError("native process identity changed during observation")
    tree = capture_owned_process_tree(pane.pid, proc_root=binding.proc_root)
    if not any(_stable_identity(item) == _stable_identity(native) for item in tree):
        raise CodexActivityError("native process left the canonical owned tree")
    try:
        header = _cli_root_header(binding.rollout_path)
    except OSError as exc:
        raise CodexActivityError("native rollout FD is no longer owned") from exc
    if header != (binding.thread_id, binding.rollout_identity):
        raise CodexActivityError(
            "native rollout FD identity changed during observation"
        )
