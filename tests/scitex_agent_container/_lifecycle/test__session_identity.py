"""Exercise the explicit lifecycle-owned Hermes transport boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.scitex_agent_container._runners import (
    test__hermes_owned_session_id as owned_fixtures,
)


@pytest.fixture
def owned_state(tmp_path):
    yield from owned_fixtures.owned_state.__wrapped__(tmp_path)


def _host_claim(owned_state):
    state, marker, descriptor, child = owned_state
    metadata = state.stat()
    namespace = Path("/proc/self/ns/pid").stat()
    fields = {
        "state_device": metadata.st_dev,
        "state_inode": metadata.st_ino,
        "pid_namespace_device": namespace.st_dev,
        "pid_namespace_inode": namespace.st_ino + 1,
    }
    marker.update(fields, schema_version=2, state_dir="/state/declared-agent")
    descriptor.update(fields, state_dir="/state/declared-agent")
    for name, value in (
        ("hermes-owned-session.json", marker),
        ("hermes-tui-gateway.json", descriptor),
        ("hermes-tui-gateway.ready.json", descriptor),
    ):
        (state / name).write_text(json.dumps(value))
    (state / "hermes-api.key").write_text("synthetic-fixture-key-0000000000")
    (state / "hermes-api.key").chmod(0o600)
    config = SimpleNamespace(name=state.name, harness="hermes", runtime="tui")
    runtime = SimpleNamespace(_state_dir=lambda _config: state)
    rows = [
        {"id": marker["live_session_id"], "session_key": marker["stored_session_id"]}
    ]
    return state, marker, descriptor, child, config, runtime, rows


def _observe(state, config, runtime, rows, *, transport=None):
    try:
        from scitex_agent_container._lifecycle._session_identity import (
            read_session_id_for,
        )

        return read_session_id_for(
            config,
            runtime,
            state,
            active_sessions_fn=transport or (lambda *_a, **_k: rows),
        )
    except (ImportError, RuntimeError) as error:
        return type(error).__name__


def test_explicit_runtime_bind_resolves_exact_live_stored_pair(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )
    # Act
    observed = _observe(state, config, runtime, rows)
    # Assert
    assert observed == "hermes-live-123"


@pytest.mark.parametrize("harness", ["codex", "claude", "", "unrecognized"])
def test_other_or_unknown_selected_harness_refuses_before_transport(
    owned_state, harness
) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )
    config.harness = harness
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("SessionEvidenceError", [])


@pytest.mark.parametrize(
    "field,value", [("state_inode", 1), ("state_device", True), ("schema_version", 1)]
)
def test_invalid_or_legacy_bind_proof_refuses_before_transport(
    owned_state, field, value
) -> None:
    # Arrange
    state, marker, _descriptor, _child, config, runtime, rows = _host_claim(owned_state)
    (state / "hermes-owned-session.json").write_text(
        json.dumps({**marker, field: value})
    )
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("SessionEvidenceError", [])


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"id": "foreign", "session_key": "hermes-stored-456"}],
        [{"id": "hermes-live-123", "session_key": "foreign"}],
        [{"id": "hermes-live-123"}],
        [{"id": "hermes-live-123", "session_key": "hermes-stored-456"}] * 2,
    ],
)
def test_absent_wrong_partial_or_duplicate_live_pair_is_unknown(
    owned_state, rows
) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, _rows = _host_claim(
        owned_state
    )
    # Act
    observed = _observe(state, config, runtime, rows)
    # Assert
    assert observed == "SessionEvidenceError"


def test_transport_failure_does_not_authorize_absence_or_expose_details(
    owned_state,
) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic-private-detail")

    # Act
    observed = _observe(state, config, runtime, rows, transport=unavailable)
    # Assert
    assert observed == "SessionEvidenceError"


def test_projection_replaced_during_transport_is_not_accepted(owned_state) -> None:
    # Arrange
    state, marker, _descriptor, _child, config, runtime, rows = _host_claim(owned_state)

    def replacing(*_args, **_kwargs):
        target = state / "hermes-owned-session.json"
        replacement = state / ".replacement"
        replacement.write_text(json.dumps(marker))
        replacement.chmod(0o600)
        os.replace(replacement, target)
        return rows

    # Act
    observed = _observe(state, config, runtime, rows, transport=replacing)
    # Assert
    assert observed == "SessionEvidenceError"


def test_runtime_owned_state_must_equal_the_supplied_state(
    owned_state, tmp_path
) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, _runtime, rows = _host_claim(
        owned_state
    )
    foreign = tmp_path / "other-owner"
    foreign.mkdir()
    runtime = SimpleNamespace(_state_dir=lambda _config: foreign)
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("SessionEvidenceError", [])


def _replace_key_with_symlink(key: Path) -> None:
    outside = key.with_name("outside.key")
    key.rename(outside)
    key.symlink_to(outside)


@pytest.mark.parametrize("kind", ["absent", "public", "symlink"])
def test_unproven_private_gateway_key_refuses_before_transport(
    owned_state, kind
) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )
    key = state / "hermes-api.key"
    mutate = {
        "absent": key.unlink,
        "public": lambda: key.chmod(0o644),
        "symlink": lambda: _replace_key_with_symlink(key),
    }
    mutate[kind]()
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("SessionEvidenceError", [])


def test_primary_sdk_marker_precedence_keeps_transport_unused(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )
    config.harness = "codex"
    (state / "session_id").write_text("retained-sdk")
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("retained-sdk", [])


def test_same_namespace_uses_actual_processes_without_transport(owned_state) -> None:
    # Arrange
    state, marker, descriptor, _child, config, runtime, rows = _host_claim(owned_state)
    namespace = Path("/proc/self/ns/pid").stat()
    marker["pid_namespace_inode"] = descriptor["pid_namespace_inode"] = namespace.st_ino
    for name, value in (
        ("hermes-owned-session.json", marker),
        ("hermes-tui-gateway.json", descriptor),
        ("hermes-tui-gateway.ready.json", descriptor),
    ):
        (state / name).write_text(json.dumps(value))
    calls = []
    # Act
    observed = _observe(
        state, config, runtime, rows, transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == ("hermes-live-123", [])


def test_gateway_key_replacement_during_transport_is_not_accepted(owned_state) -> None:
    # Arrange
    state, _marker, _descriptor, _child, config, runtime, rows = _host_claim(
        owned_state
    )

    def replacing(*_args, **_kwargs):
        target = state / "hermes-api.key"
        replacement = state / ".key-replacement"
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, target)
        return rows

    # Act
    observed = _observe(state, config, runtime, rows, transport=replacing)
    # Assert
    assert observed == "SessionEvidenceError"


def test_confirmed_absence_returns_none_without_transport(tmp_path) -> None:
    # Arrange
    config = SimpleNamespace(harness="codex", runtime="tui")
    calls = []
    runtime = SimpleNamespace(_state_dir=lambda _config: tmp_path)
    # Act
    observed = _observe(
        tmp_path, config, runtime, [], transport=lambda *_a, **_k: calls.append(1)
    )
    # Assert
    assert (observed, calls) == (None, [])
