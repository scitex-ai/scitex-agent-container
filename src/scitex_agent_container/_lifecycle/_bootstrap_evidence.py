"""Read-only authority checks for an already admitted native startup turn."""

from __future__ import annotations

import hashlib
import math
import os
import socket
import stat
from dataclasses import fields
from pathlib import Path

from ..config import load_config
from ..runtimes._boot_recovery import with_missed_input_recovery
from ..runtimes._codex_activity_binding import bind_codex_runtime
from ..runtimes._codex_startup_admission import StartupAdmissionProbe
from ..runtimes.tui_session import TuiSessionRuntime, session_name_for


def source_mission(config) -> str:
    """Use the same source prompt compilation as normal Codex startup."""
    prompts = list(config.startup_prompts or [])
    if not prompts or any(type(prompt) is not str or not prompt for prompt in prompts):
        raise RuntimeError("bootstrap completion requires nonempty source prompts")
    return "\n\n".join(with_missed_input_recovery(prompts))


def mission_copy_count(actual: str, expected: str) -> int:
    """Only one or exactly two whole identical source missions are admissible."""
    if type(actual) is not str or not expected:
        raise RuntimeError("bootstrap mission is not the complete source mandate")
    for count in (1, 2):
        if actual == expected * count:
            return count
    raise RuntimeError("bootstrap mission is not one or two complete source mandates")


def _read_spec(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1_024 * 1_024:
            raise RuntimeError("bootstrap specification is not a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(1_024 * 1_024 + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)

    def identity(row):
        return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns

    digest = hashlib.sha256(raw).hexdigest()
    if identity(before) != identity(after) or identity(path.lstat()) != identity(after):
        raise RuntimeError("bootstrap formal specification changed")
    return identity(after), digest


def spec_snapshot(config, expected_sha256: str) -> tuple[Path, tuple, str]:
    """Bind current formal source bytes and reject changed selected configuration."""
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise ValueError("bootstrap completion requires an exact source SHA256")
    path = Path(config.config_path).resolve(strict=True)
    before, digest = _read_spec(path)
    if digest != expected_sha256:
        raise RuntimeError("bootstrap formal specification changed")
    current = load_config(str(path))
    # Normal launch may resolve an automatic A2A port and add transport env.
    # All other declared fields, including account/model/session/hooks, remain
    # equal. Recovery never applies an engine/session override or workdir move.
    for field in fields(current):
        if field.name not in {"config_path", "env", "a2a"}:
            if getattr(config, field.name) != getattr(current, field.name):
                raise RuntimeError(
                    f"bootstrap selected configuration changed: {field.name}"
                )
    if any(config.env.get(key) != value for key, value in current.env.items()):
        raise RuntimeError("bootstrap declared environment changed")
    for field in fields(current.a2a):
        if field.name != "port" or current.a2a.port != "auto":
            if getattr(config.a2a, field.name) != getattr(current.a2a, field.name):
                raise RuntimeError("bootstrap declared A2A authority changed")
    # The parser's second read must still refer to the bound byte snapshot.
    if _read_spec(path) != (before, digest):
        raise RuntimeError("bootstrap formal specification changed while loading")
    return path, before, digest


def admitted_native(config, runtime, admission, *, observed_at: float):
    """Require real local pane/FD/JSONL proof, never a virtual truth callback."""
    if type(admission) is not StartupAdmissionProbe:
        raise RuntimeError(
            "bootstrap completion requires a genuine native admission probe"
        )
    if type(runtime) is not TuiSessionRuntime or config.harness != "codex":
        raise RuntimeError("bootstrap completion supports only the native Codex TUI")
    if (
        type(observed_at) not in {int, float}
        or not math.isfinite(observed_at)
        or type(admission.issued_at) not in {int, float}
        or not math.isfinite(admission.issued_at)
        or admission.issued_at > observed_at
    ):
        raise RuntimeError("bootstrap admission has unknown or future timing")
    record = admission.record
    if (
        record.get("id") != config.env.get("SAC_INSTANCE_UUID")
        or record.get("name") != config.name
        or record.get("host") != socket.gethostname()
        or record.get("screen") != session_name_for(config)
        or runtime.agent_pid(config) != record.get("pid")
    ):
        raise RuntimeError("bootstrap launch or current pane authority differs")
    copies = mission_copy_count(admission.mission, source_mission(config))
    if not admission.observed(observed_at=observed_at):
        raise RuntimeError("bootstrap source mission has no genuine native admission")
    binding = bind_codex_runtime(
        record,
        instance_id=record["id"],
        agent_name=config.name,
        host=record["host"],
        proc_root=admission.proc_root,
    )
    return binding, copies
