"""Exercise owned-marker fallback with real files and process incarnations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._runners import _session_id as sid


def _start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[19])


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _read_outcome(state: Path):
    try:
        return sid.read_session_id(state)
    except RuntimeError as error:
        return type(error).__name__


@pytest.fixture
def owned_state(tmp_path: Path):
    state = tmp_path / "hermes-agent"
    state.mkdir(mode=0o700)
    child = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
    )
    descriptor = {
        "harness": "hermes",
        "state_dir": str(state.resolve()),
        "generation": "0123456789abcdef0123456789abcdef",
        "owner_pid": os.getpid(),
        "owner_start_ticks": _start_ticks(os.getpid()),
        "pid": child.pid,
        "gateway_start_ticks": _start_ticks(child.pid),
        "port": 19888,
        "state_device": state.stat().st_dev,
        "state_inode": state.stat().st_ino,
        "pid_namespace_device": Path("/proc/self/ns/pid").stat().st_dev,
        "pid_namespace_inode": Path("/proc/self/ns/pid").stat().st_ino,
    }
    marker = {
        "schema_version": 2,
        "harness": "hermes",
        "state_dir": str(state.resolve()),
        "generation": descriptor["generation"],
        "owner_pid": descriptor["owner_pid"],
        "owner_start_ticks": descriptor["owner_start_ticks"],
        "gateway_pid": child.pid,
        "gateway_start_ticks": descriptor["gateway_start_ticks"],
        "live_session_id": "hermes-live-123",
        "stored_session_id": "hermes-stored-456",
        "state_device": descriptor["state_device"],
        "state_inode": descriptor["state_inode"],
        "pid_namespace_device": descriptor["pid_namespace_device"],
        "pid_namespace_inode": descriptor["pid_namespace_inode"],
    }
    _write(state / "hermes-tui-gateway.json", descriptor)
    _write(state / "hermes-tui-gateway.ready.json", descriptor)
    _write(state / "hermes-owned-session.json", marker)
    try:
        yield state, marker, descriptor, child
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_owned_hermes_marker_resolves_when_sdk_marker_is_absent(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "hermes-live-123"


def test_sdk_marker_keeps_precedence_over_owned_hermes_marker(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    sid.write_session_id(state, "sdk-current")
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "sdk-current"


@pytest.mark.parametrize("content", ["", "   "])
def test_present_empty_sdk_marker_never_adopts_a_different_harness(
    owned_state, content
) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "session_id").write_text(content)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_sdk_marker_directory_is_unknown_instead_of_hermes_absence(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "session_id").mkdir()
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


@pytest.mark.parametrize(
    "field,value",
    [
        ("harness", "codex"),
        ("harness", "claude"),
        ("schema_version", True),
        ("schema_version", 1),
        ("live_session_id", 123),
        ("stored_session_id", None),
        ("live_session_id", ""),
        ("live_session_id", "not a session"),
        ("owner_pid", True),
        ("owner_start_ticks", -1),
        ("gateway_pid", 0),
        ("generation", "foreign-generation"),
        ("state_dir", "/other-agent/state"),
    ],
)
def test_malformed_foreign_and_other_harness_claims_are_refused(
    owned_state, field, value
) -> None:
    # Arrange
    state, marker, _descriptor, _child = owned_state
    _write(state / "hermes-owned-session.json", {**marker, field: value})
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


@pytest.mark.parametrize(
    "payload", [None, [], {"live_session_id": "unowned", "stored_session_id": "legacy"}]
)
def test_legacy_and_nonobject_owned_markers_are_unknown(owned_state, payload) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    _write(state / "hermes-owned-session.json", payload)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_malformed_json_is_refused_without_changing_the_marker(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "hermes-owned-session.json").write_text("{")
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_missing_ready_projection_refuses_owned_marker(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "hermes-tui-gateway.ready.json").unlink()
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_replaced_ready_generation_refuses_owned_marker(owned_state) -> None:
    # Arrange
    state, _marker, descriptor, _child = owned_state
    _write(
        state / "hermes-tui-gateway.ready.json",
        {**descriptor, "generation": "new-owner"},
    )
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_gateway_process_incarnation_mismatch_refuses_owned_marker(owned_state) -> None:
    # Arrange
    state, marker, _descriptor, _child = owned_state
    _write(
        state / "hermes-owned-session.json",
        {**marker, "gateway_start_ticks": marker["gateway_start_ticks"] + 1},
    )
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_dead_gateway_refuses_the_retained_complete_claim(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, child = owned_state
    child.terminate()
    child.wait(timeout=2)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_marker_copied_to_another_agent_is_not_owned_there(
    owned_state, tmp_path: Path
) -> None:
    # Arrange
    state, marker, descriptor, _child = owned_state
    other = tmp_path / "foreign-agent"
    other.mkdir(mode=0o700)
    for name, value in [
        ("hermes-owned-session.json", marker),
        ("hermes-tui-gateway.json", descriptor),
        ("hermes-tui-gateway.ready.json", descriptor),
    ]:
        _write(other / name, value)
    # Act
    result = _read_outcome(other)
    # Assert
    assert result == "SessionEvidenceError"


def test_symlink_owned_marker_cannot_introduce_foreign_identity(
    owned_state, tmp_path: Path
) -> None:
    # Arrange
    state, marker, _descriptor, _child = owned_state
    target = tmp_path / "outside-marker.json"
    _write(target, marker)
    (state / "hermes-owned-session.json").unlink()
    (state / "hermes-owned-session.json").symlink_to(target)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_public_mode_owned_marker_is_not_a_private_owner_projection(
    owned_state,
) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "hermes-owned-session.json").chmod(0o644)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_legacy_unknown_owner_cannot_silently_seed_a_replacement_session(
    owned_state,
) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._session_seed import seed_pinned_session_id

    state, marker, _descriptor, _child = owned_state
    legacy = {
        "live_session_id": marker["live_session_id"],
        "stored_session_id": marker["stored_session_id"],
    }
    path = state / "hermes-owned-session.json"
    _write(path, legacy)
    original = path.read_bytes()
    config = SimpleNamespace(
        name=state.name,
        claude=SimpleNamespace(session="resume", resume_id="replacement-session"),
    )
    runtime = SimpleNamespace(_state_dir=lambda _config: state)
    # Act
    try:
        outcome = seed_pinned_session_id(config, runtime)
    except RuntimeError as error:
        outcome = type(error).__name__
    # Assert
    assert (outcome, path.read_bytes(), (state / "session_id").exists()) == (
        "SessionEvidenceError",
        original,
        False,
    )


def test_official_current_producer_round_trip_proves_owned_session(owned_state) -> None:
    # Arrange
    from scitex_agent_container._runners._hermes_owned_session import (
        owned_session_projection,
    )
    from scitex_agent_container.runtimes import _hermes_tui_owner as owner

    state, _marker, descriptor, child = owned_state
    owner._publish_gateway_state(
        state, generation=descriptor["generation"], port=19888, gateway_pid=child.pid
    )
    # Act
    projection = owned_session_projection(
        state, {"id": "producer-live", "session_key": "producer-stored"}
    )
    owner._atomic_json(state / "hermes-owned-session.json", projection)
    result = sid.read_session_id(state)
    # Assert
    assert result == "producer-live"


def test_stale_but_internally_matching_process_claim_still_refuses(owned_state) -> None:
    # Arrange
    state, marker, descriptor, _child = owned_state
    stale_ticks = descriptor["gateway_start_ticks"] - 1
    stale_descriptor = {**descriptor, "gateway_start_ticks": stale_ticks}
    _write(state / "hermes-tui-gateway.json", stale_descriptor)
    _write(state / "hermes-tui-gateway.ready.json", stale_descriptor)
    _write(
        state / "hermes-owned-session.json",
        {**marker, "gateway_start_ticks": stale_ticks},
    )
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_unknown_owned_twin_candidate_cannot_seed_parent_context(owned_state) -> None:
    # Arrange
    from scitex_agent_container._lifecycle._twin import seed_twin_from_parent

    state, _marker, _descriptor, _child = owned_state
    path = state / "hermes-owned-session.json"
    _write(
        path,
        {"live_session_id": "retained-twin", "stored_session_id": "retained-stored"},
    )
    original = path.read_bytes()
    config = SimpleNamespace(name=state.name, env={"SAC_FORK_PARENT": "foreign-parent"})
    runtime = SimpleNamespace(_state_dir=lambda _config: state)
    # Act
    try:
        outcome = seed_twin_from_parent(config, runtime)
    except RuntimeError as error:
        outcome = type(error).__name__
    # Assert
    assert (outcome, path.read_bytes(), (state / "session_id").exists()) == (
        "SessionEvidenceError",
        original,
        False,
    )


def test_owned_marker_fifo_is_refused_without_a_blocking_read(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    path = state / "hermes-owned-session.json"
    path.unlink()
    os.mkfifo(path, 0o600)
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_current_gateway_without_an_owned_id_is_unknown_instead_of_fresh(
    owned_state,
) -> None:
    # Arrange
    state, _marker, _descriptor, _child = owned_state
    (state / "hermes-owned-session.json").unlink()
    # Act
    result = _read_outcome(state)
    # Assert
    assert result == "SessionEvidenceError"


def test_producer_does_not_turn_a_numeric_rpc_id_into_identity(owned_state) -> None:
    # Arrange
    from scitex_agent_container._runners._hermes_owned_session import (
        owned_session_projection,
    )

    state, _marker, _descriptor, _child = owned_state
    # Act
    try:
        outcome = owned_session_projection(state, {"id": 123, "session_key": "stored"})
    except RuntimeError as error:
        outcome = type(error).__name__
    # Assert
    assert outcome == "SessionEvidenceError"
