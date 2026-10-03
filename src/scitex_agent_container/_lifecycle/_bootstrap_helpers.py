"""Physical and persistent-unit fences for same-process bootstrap helpers."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .._runners._tmux._process_group import _identity
from ..runtimes._tui_turn_bridge_lifecycle import (
    MODULE_PATH,
    resolved_a2a_host,
    resolved_a2a_port,
)


@dataclass(frozen=True)
class ExistingBridge:
    """Independently reviewed marker and live bridge incarnation; no death guess."""

    pid: int
    birth: int
    marker_identity: tuple[int, int, int, int]
    marker_sha256: str


@dataclass(frozen=True)
class RetainedHelper:
    """A retainer's exact live process/unit result, independently read back below."""

    pid: int
    birth: int
    uid: int
    control_group: str
    unit: str
    invocation_id: str


def unit_properties(unit: str) -> dict:
    """Read the declared systemd unit; never stop/start/change it."""
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=ControlGroup,InvocationID,ActiveState,RuntimeMaxUSec,PartOf,BindsTo,Requires",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def bridge_health(config) -> bool:
    """Bounded GET-only local bridge readiness; TCP alone is not an identity proof."""
    host = resolved_a2a_host(config)
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise RuntimeError("existing bridge readiness requires declared IPv4 loopback")
    connection = http.client.HTTPConnection(
        "127.0.0.1", resolved_a2a_port(config), timeout=3
    )
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(4_097)
        if response.status != 200 or len(raw) > 4_096:
            return False
    finally:
        connection.close()
    return json.loads(raw) == {"status": "ok", "agent": config.name}


def verify_existing_bridge(
    config, marker: Path, authority, *, proc_root: Path, health_fn
):
    """Require exact marker bytes, owned birth, normal source argv, and GET health."""
    if type(authority) is not ExistingBridge:
        raise RuntimeError("existing bridge requires exact typed ownership evidence")
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        meta = os.fstat(fd)

        def identity(row):
            return row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns

        if (
            not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.getuid()
            or not 0 < meta.st_size <= 32
            or identity(meta) != authority.marker_identity
        ):
            raise RuntimeError("existing bridge marker ownership changed")
        raw = os.read(fd, 33)
        if (
            raw != str(authority.pid).encode()
            or hashlib.sha256(raw).hexdigest() != authority.marker_sha256
            or identity(os.fstat(fd)) != authority.marker_identity
            or identity(marker.lstat()) != authority.marker_identity
        ):
            raise RuntimeError("existing bridge marker bytes changed")
    finally:
        os.close(fd)
    process = _identity(authority.pid, proc_root=proc_root)
    if (
        process is None
        or process.state == "Z"
        or process.uid != os.getuid()
        or process.start_time != authority.birth
    ):
        raise RuntimeError("existing bridge live ownership changed")
    with (proc_root / str(process.pid) / "cmdline").open("rb") as stream:
        raw_argv = stream.read(8_193)
    if len(raw_argv) > 8_192:
        raise RuntimeError("existing bridge argv is unbounded")
    argv = raw_argv.rstrip(b"\0").decode().split("\0")
    expected = [
        "-m",
        MODULE_PATH,
        "--config-path",
        config.config_path,
        "--port",
        str(resolved_a2a_port(config)),
        "--host",
        resolved_a2a_host(config),
    ]
    if len(argv) != 9 or argv[1:] != expected or not Path(argv[0]).is_absolute():
        raise RuntimeError(
            "existing bridge normal module/config/port authority differs"
        )
    if health_fn(config) is not True:
        raise RuntimeError("existing bridge has no actual GET-only readiness")
    current = _identity(process.pid, proc_root=proc_root)
    if (
        current is None
        or current.state == "Z"
        or any(
            getattr(current, key) != getattr(process, key)
            for key in ("pid", "start_time", "uid", "control_group")
        )
    ):
        raise RuntimeError("existing bridge changed during readiness read")
    return process


def verify_retention(before, retained, *, controller, proc_root: Path, show_unit):
    """Verify persistent lifetime without changing a listener or trusting setsid."""
    if type(retained) is not RetainedHelper:
        raise RuntimeError("helper has no typed independent retention receipt")
    current = _identity(before.pid, proc_root=proc_root)
    if (
        current is None
        or current.state == "Z"
        or (retained.pid, retained.birth, retained.uid)
        != (before.pid, before.start_time, before.uid)
        or (current.pid, current.start_time, current.uid)
        != (before.pid, before.start_time, before.uid)
        or current.control_group != retained.control_group
        or not retained.control_group
        or retained.control_group == "/"
        or PurePosixPath(retained.control_group).name != retained.unit
        or retained.unit.startswith("-")
        or not retained.unit.endswith((".scope", ".service"))
        or not all(char.isalnum() or char in "_.-:" for char in retained.unit)
        or len(retained.invocation_id) != 32
        or any(char not in "0123456789abcdef" for char in retained.invocation_id)
        or current.control_group == controller.control_group
        or current.control_group.startswith(controller.control_group.rstrip("/") + "/")
    ):
        raise RuntimeError("helper remains unowned or in the finite controller cgroup")
    shown = show_unit(retained.unit)
    controller_unit = PurePosixPath(controller.control_group).name
    if (
        shown.get("ControlGroup") != retained.control_group
        or shown.get("InvocationID") != retained.invocation_id
        or shown.get("ActiveState") != "active"
        or shown.get("RuntimeMaxUSec") != "infinity"
        or any(
            controller_unit in shown.get(key, "").split()
            for key in ("PartOf", "BindsTo", "Requires")
        )
    ):
        raise RuntimeError(
            "helper persistent unit readback is unknown or controller-dependent"
        )
    return retained
