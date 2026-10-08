"""Local ownership evidence for the official Hermes session projection."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

OWNED_FILE = "hermes-owned-session.json"
GATEWAY_FILE = "hermes-tui-gateway.json"
READY_FILE = "hermes-tui-gateway.ready.json"
_MAX_BYTES = 64 * 1024
_SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


class SessionEvidenceError(RuntimeError):
    """A retained session candidate exists but its ownership is unproven."""


def _refuse(reason: str) -> None:
    raise SessionEvidenceError(f"Hermes session evidence is unproven: {reason}")


def _file_identity(metadata: os.stat_result) -> tuple:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _private_json_snapshot(state_dir: Path, name: str) -> tuple[dict, tuple]:
    try:
        descriptor = os.open(
            state_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != state_dir.stat().st_uid
                or metadata.st_uid != os.geteuid()
                or metadata.st_size > _MAX_BYTES
            ):
                _refuse("projection-file-ownership-or-type")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                encoded = stream.read(_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(encoded) > _MAX_BYTES:
            _refuse("projection-too-large")
        value = json.loads(encoded)
    except (OSError, ValueError, UnicodeError) as error:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: projection-unavailable-or-malformed"
        ) from error
    if not isinstance(value, dict):
        _refuse("projection-not-object")
    return value, _file_identity(metadata)


def pid_namespace_identity() -> tuple[int, int]:
    try:
        metadata = Path("/proc/self/ns/pid").stat()
        return metadata.st_dev, metadata.st_ino
    except OSError as error:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: pid-namespace-unavailable"
        ) from error


def has_owned_session_candidate(state_dir: Path) -> bool:
    for name in (OWNED_FILE, GATEWAY_FILE, READY_FILE):
        try:
            (state_dir / name).lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise SessionEvidenceError(
                "Hermes session evidence is unproven: candidate-unavailable"
            ) from error
        return True
    return False


def process_start_ticks(pid: int, *, uid: int | None = None) -> int | None:
    """Read one live process incarnation; unavailable metadata stays unknown."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        root = Path(f"/proc/{pid}")
        if root.stat().st_uid != (os.geteuid() if uid is None else uid):
            return None
        fields = (root / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        if fields[0] in {"Z", "X", "x"} or ticks <= 0:
            return None
        return ticks
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def gateway_owner_evidence(
    state_dir: Path, *, owner_pid: int, gateway_pid: int
) -> dict:
    """Extend the existing descriptor only when both incarnations are known."""
    owner_start = process_start_ticks(owner_pid)
    gateway_start = process_start_ticks(gateway_pid)
    if owner_start is None or gateway_start is None or owner_pid == gateway_pid:
        return {}
    metadata = state_dir.stat()
    namespace_device, namespace_inode = pid_namespace_identity()
    return {
        "harness": "hermes",
        "state_dir": str(state_dir.resolve()),
        "owner_start_ticks": owner_start,
        "gateway_start_ticks": gateway_start,
        "state_device": metadata.st_dev,
        "state_inode": metadata.st_ino,
        "pid_namespace_device": namespace_device,
        "pid_namespace_inode": namespace_inode,
    }


def _gateway_evidence(
    state_dir: Path, *, host_bind: bool = False
) -> tuple[dict, tuple]:
    descriptor, descriptor_stat = _private_json_snapshot(state_dir, GATEWAY_FILE)
    ready, ready_stat = _private_json_snapshot(state_dir, READY_FILE)
    fields = (
        "generation",
        "owner_pid",
        "pid",
        "port",
        "harness",
        "state_dir",
        "owner_start_ticks",
        "gateway_start_ticks",
        "state_device",
        "state_inode",
        "pid_namespace_device",
        "pid_namespace_inode",
    )
    if any(name not in descriptor for name in fields) or any(
        descriptor.get(name) != ready.get(name)
        or type(descriptor.get(name)) is not type(ready.get(name))
        for name in fields
    ):
        _refuse("gateway-ready-generation-mismatch")
    if (
        descriptor["harness"] != "hermes"
        or not isinstance(descriptor["state_dir"], str)
        or not Path(descriptor["state_dir"]).is_absolute()
    ):
        _refuse("gateway-harness-or-state-path")
    if not host_bind and descriptor["state_dir"] != str(state_dir.resolve()):
        _refuse("gateway-state-path-needs-owned-runtime")
    metadata = state_dir.stat()
    if any(
        type(descriptor[field]) is not int
        for field in (
            "state_device",
            "state_inode",
            "pid_namespace_device",
            "pid_namespace_inode",
        )
    ):
        _refuse("gateway-bind-or-namespace-shape")
    if (descriptor["state_device"], descriptor["state_inode"]) != (
        metadata.st_dev,
        metadata.st_ino,
    ):
        _refuse("gateway-state-bind-identity")
    if descriptor["pid_namespace_device"] < 0 or descriptor["pid_namespace_inode"] <= 0:
        _refuse("gateway-pid-namespace-shape")
    if not isinstance(descriptor["generation"], str) or not re.fullmatch(
        r"[a-f0-9]{32}", descriptor["generation"]
    ):
        _refuse("gateway-generation-shape")
    if type(descriptor["port"]) is not int or not 0 < descriptor["port"] < 65536:
        _refuse("gateway-port-shape")
    for field in ("owner_pid", "pid", "owner_start_ticks", "gateway_start_ticks"):
        if type(descriptor[field]) is not int or descriptor[field] <= 0:
            _refuse("gateway-incarnation-shape")
    if descriptor["owner_pid"] == descriptor["pid"]:
        _refuse("gateway-owner-is-not-distinct")
    namespace = (descriptor["pid_namespace_device"], descriptor["pid_namespace_inode"])
    if namespace == pid_namespace_identity():
        for pid_field, start_field in (
            ("owner_pid", "owner_start_ticks"),
            ("pid", "gateway_start_ticks"),
        ):
            if (
                process_start_ticks(descriptor[pid_field], uid=metadata.st_uid)
                != descriptor[start_field]
            ):
                _refuse("gateway-process-incarnation-unavailable")
    elif not host_bind:
        _refuse("gateway-pid-namespace-needs-owned-runtime")
    return descriptor, (descriptor_stat, ready_stat)


def owned_session_projection(state_dir: Path, session: dict) -> dict:
    """Keep old consumer fields while binding new proof to this official owner."""
    value = {
        "live_session_id": str(session.get("id") or ""),
        "stored_session_id": str(session.get("session_key") or session.get("id") or ""),
    }
    try:
        descriptor, _snapshot = _gateway_evidence(state_dir)
    except SessionEvidenceError:
        # A legacy descriptor remains a legacy projection, never new proof.
        return value
    if descriptor["owner_pid"] != os.getpid():
        _refuse("projection-writer-is-not-current-owner")
    live_id = session.get("id")
    stored_id = session.get("session_key")
    if stored_id is None or stored_id == "":
        stored_id = live_id
    if any(
        not isinstance(value, str) or not _SESSION_ID.fullmatch(value)
        for value in (live_id, stored_id)
    ):
        _refuse("producer-session-id-shape")
    value.update(
        {
            "schema_version": 2,
            "harness": "hermes",
            "state_dir": descriptor["state_dir"],
            "generation": descriptor["generation"],
            "owner_pid": descriptor["owner_pid"],
            "owner_start_ticks": descriptor["owner_start_ticks"],
            "gateway_pid": descriptor["pid"],
            "gateway_start_ticks": descriptor["gateway_start_ticks"],
            "state_device": descriptor["state_device"],
            "state_inode": descriptor["state_inode"],
            "pid_namespace_device": descriptor["pid_namespace_device"],
            "pid_namespace_inode": descriptor["pid_namespace_inode"],
        }
    )
    return value


def _owned_evidence(state_dir: Path, *, host_bind: bool = False) -> tuple | None:
    try:
        (state_dir / OWNED_FILE).lstat()
    except FileNotFoundError:
        for name in (GATEWAY_FILE, READY_FILE):
            try:
                (state_dir / name).lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise SessionEvidenceError(
                    "Hermes session evidence is unproven: gateway-candidate-unavailable"
                ) from error
            _refuse("gateway-candidate-has-no-owned-session")
        return None
    except OSError as error:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: candidate-unavailable"
        ) from error
    marker, marker_stat = _private_json_snapshot(state_dir, OWNED_FILE)
    if type(marker.get("schema_version")) is not int or marker["schema_version"] != 2:
        _refuse("legacy-or-unsupported-owned-marker")
    if marker.get("harness") != "hermes":
        _refuse("owned-marker-harness")
    for field in ("live_session_id", "stored_session_id"):
        if not isinstance(marker.get(field), str) or not _SESSION_ID.fullmatch(
            marker[field]
        ):
            _refuse("owned-session-id-shape")
    descriptor, gateway_stat = _gateway_evidence(state_dir, host_bind=host_bind)
    for marker_field, descriptor_field in (
        ("generation", "generation"),
        ("owner_pid", "owner_pid"),
        ("owner_start_ticks", "owner_start_ticks"),
        ("gateway_pid", "pid"),
        ("gateway_start_ticks", "gateway_start_ticks"),
        ("state_dir", "state_dir"),
        ("state_device", "state_device"),
        ("state_inode", "state_inode"),
        ("pid_namespace_device", "pid_namespace_device"),
        ("pid_namespace_inode", "pid_namespace_inode"),
    ):
        value = marker.get(marker_field)
        if (
            type(value) is not type(descriptor[descriptor_field])
            or value != descriptor[descriptor_field]
        ):
            _refuse("owned-marker-incarnation-mismatch")
    return marker, descriptor, marker_stat, gateway_stat


def read_owned_hermes_session_id(state_dir: Path) -> str | None:
    """Path-only proof is local; namespace aliases need an explicit runtime."""
    evidence = _owned_evidence(state_dir)
    if evidence is None:
        return None
    if _owned_evidence(state_dir) != evidence:
        _refuse("owned-marker-changed-during-observation")
    return evidence[0]["live_session_id"]


def _private_gateway_key_identity(state_dir: Path) -> tuple:
    try:
        metadata = (state_dir / "hermes-api.key").lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != state_dir.stat().st_uid
            or metadata.st_uid != os.geteuid()
            or not 16 <= metadata.st_size <= 4096
        ):
            _refuse("gateway-key-ownership-or-type")
        return _file_identity(metadata)
    except OSError as error:
        raise SessionEvidenceError(
            "Hermes session evidence is unproven: gateway-key-unavailable"
        ) from error


def read_runtime_owned_hermes_session_id(
    state_dir: Path, *, active_sessions_fn
) -> str | None:
    """Called only after lifecycle verifies selected harness and runtime state."""
    evidence = _owned_evidence(state_dir, host_bind=True)
    if evidence is None:
        return None
    marker, descriptor, _marker_stat, _gateway_stat = evidence
    namespace = (descriptor["pid_namespace_device"], descriptor["pid_namespace_inode"])
    observed_namespace = pid_namespace_identity()
    key_stat = _private_gateway_key_identity(state_dir)
    if namespace != observed_namespace:
        try:
            rows = active_sessions_fn(state_dir, timeout_s=2.0)
        except Exception:
            raise SessionEvidenceError(
                "Hermes session evidence is unproven: authenticated-live-session-unavailable"
            ) from None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            _refuse("authenticated-live-session-shape")
        live = [row for row in rows if row.get("id") == marker["live_session_id"]]
        if len(live) != 1 or live[0].get("session_key") != marker["stored_session_id"]:
            _refuse("authenticated-live-session-pair-mismatch")
    if (
        _private_gateway_key_identity(state_dir) != key_stat
        or pid_namespace_identity() != observed_namespace
    ):
        _refuse("gateway-key-or-reader-namespace-changed")
    if _owned_evidence(state_dir, host_bind=True) != evidence:
        _refuse("owned-marker-changed-during-observation")
    return marker["live_session_id"]
