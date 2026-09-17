"""Adversarial guards for runtime identity authority and privacy."""

from __future__ import annotations

import json

from scitex_agent_container._lifecycle._runtime_identity import (
    bind_active_instance,
    resolve_bound_birth_records,
    resolve_runtime_identity,
)
from scitex_agent_container._lifecycle._status import (
    _resolve_account,
)
from scitex_agent_container._lifecycle._status import (
    _runtime_identity as status_runtime_identity,
)
from scitex_agent_container._listen.server import _runtime_liveness
from scitex_agent_container.cli_pkg._helpers._agent_list_account import (
    _safe_account_for,
)
from scitex_agent_container.config import AgentConfig


def _birth(**updates) -> dict:
    snapshot = {
        "runtime": "tui",
        "harness": "anthropic",
        "engine_key": "engine-a",
        "model": "model-a",
        "claude": {"account": "", "credentials_file": "", "provider": None},
    }
    snapshot.update(updates)
    return {"compiled_spec_json": json.dumps(snapshot)}


def test_hermes_never_treats_legacy_claude_account_as_its_identity() -> None:
    # Arrange
    birth = _birth(
        harness="hermes",
        claude={
            "account": "private-tenant",
            "credentials_file": "/accounts/private-tenant/.credentials.json",
            "provider": None,
        },
    )

    # Act
    identity = resolve_runtime_identity(None, running=True, birth_record=birth)

    # Assert
    assert identity["auth_identity"] == "unknown"


def test_codex_never_treats_legacy_claude_account_as_its_identity() -> None:
    # Arrange
    birth = _birth(
        harness="codex",
        claude={"account": "private-tenant", "provider": None},
    )

    # Act
    identity = resolve_runtime_identity(None, running=True, birth_record=birth)

    # Assert
    assert identity["auth_identity"] == "unknown"


def test_non_claude_harness_never_resolves_legacy_stored_credential() -> None:
    # Arrange
    config = AgentConfig(name="alpha", harness="hermes")
    config.claude.account = "private-tenant"
    config.claude.credentials_file = "/accounts/private-tenant/.credentials.json"

    # Act
    list_value = _safe_account_for(config)
    status_value = _resolve_account(config)

    # Assert
    assert (list_value, status_value) == ("unknown", "unknown")


def test_api_key_identity_is_opaque_and_reveals_no_env_fragment() -> None:
    # Arrange
    env_name = "CUSTOMER_TENANT_DEEPSEEK_PRODUCTION_KEY"
    birth = _birth(
        harness="hermes",
        claude={"provider": {"auth_token_env": env_name, "source": "tenant-blue"}},
    )

    # Act
    identity = resolve_runtime_identity(None, running=True, birth_record=birth)
    rendered = json.dumps(identity)

    # Assert
    assert identity["auth_identity"] == "api-key" and env_name not in rendered and "tenant-blue" not in rendered


def test_anthropic_does_not_derive_identity_from_credentials_parent() -> None:
    # Arrange
    birth = _birth(
        harness="anthropic",
        claude={
            "account": "",
            "credentials_file": "/arbitrary/private-customer/.credentials.json",
            "provider": None,
        },
    )

    # Act
    identity = resolve_runtime_identity(None, running=True, birth_record=birth)

    # Assert
    assert identity["auth_identity"] == "unknown"


def test_anthropic_accepts_only_a_sanitized_persisted_account_label() -> None:
    # Arrange
    safe_birth = _birth(claude={"account": "team-max", "provider": None})
    unsafe_birth = _birth(claude={"account": "team/max[link]", "provider": None})

    # Act
    safe = resolve_runtime_identity(
        None,
        running=True,
        birth_record=safe_birth,
    )
    unsafe = resolve_runtime_identity(
        None,
        running=True,
        birth_record=unsafe_birth,
    )

    # Assert
    assert safe["auth_identity"] == "claude-code:team-max" and unsafe["auth_identity"] == "unknown"


def test_duplicate_active_rows_bind_only_to_the_persisted_incarnation() -> None:
    # Arrange
    rows = [
        {"id": "newest-wrong", "name": "alpha", "host": "node-a", "pid": 222, "remote": False},
        {"id": "bound-real", "name": "alpha", "host": "node-a", "pid": 111, "remote": False},
    ]

    # Act
    bound = bind_active_instance(
        "alpha",
        rows,
        local_host="node-a",
        marker_id="bound-real",
        pid=111,
        session="",
        heartbeat=None,
    )

    # Assert
    assert bound is rows[1]


def test_duplicate_active_rows_without_incarnation_evidence_do_not_guess() -> None:
    # Arrange
    rows = [
        {"id": "newest-wrong", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "older-wrong", "name": "alpha", "host": "node-a", "remote": False},
    ]

    # Act
    bound = bind_active_instance(
        "alpha",
        rows,
        local_host="node-a",
        marker_id=None,
        pid=None,
        session="",
        heartbeat=None,
    )

    # Assert
    assert bound is None


def test_conflicting_marker_and_heartbeat_refuse_birth_authority() -> None:
    # Arrange
    rows = [
        {"id": "marker-id", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "beat-id", "name": "alpha", "host": "node-a", "remote": False},
    ]

    # Act
    bound = bind_active_instance(
        "alpha",
        rows,
        local_host="node-a",
        marker_id="marker-id",
        pid=None,
        session="",
        heartbeat={"incarnation_id": "beat-id"},
    )

    # Assert
    assert bound is None


def test_bound_births_use_one_batch_read_and_never_newest_name_host() -> None:
    # Arrange
    active = [
        {"id": "newest-wrong", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "alpha-real", "name": "alpha", "host": "node-a", "remote": False},
        {"id": "beta-real", "name": "beta", "host": "node-a", "remote": False},
    ]
    calls: list[tuple[str, ...]] = []

    def evidence(name: str, row: dict) -> dict:
        return {
            "marker_id": f"{name}-real",
            "pid": row.get("pid"),
            "session": row.get("screen"),
            "heartbeat": None,
        }

    def births(ids: tuple[str, ...]) -> dict[str, dict]:
        calls.append(ids)
        return {value: {"incarnation_id": value} for value in ids}

    # Act
    records = resolve_bound_birth_records(
        [{"name": "alpha"}, {"name": "beta"}],
        active_instances=active,
        local_host="node-a",
        evidence_reader=evidence,
        birth_reader=births,
    )

    # Assert
    assert records == {
        "alpha": {"incarnation_id": "alpha-real"},
        "beta": {"incarnation_id": "beta-real"},
    } and calls == [("alpha-real", "beta-real")]


def test_unbound_running_row_triggers_no_birth_read() -> None:
    # Arrange
    calls = 0

    def births(ids: tuple[str, ...]) -> dict[str, dict]:
        nonlocal calls
        calls += 1
        return {}

    # Act
    records = resolve_bound_birth_records(
        [{"name": "alpha"}],
        active_instances=[
            {"id": "stale-a", "name": "alpha", "host": "node-a", "remote": False},
            {"id": "stale-b", "name": "alpha", "host": "node-a", "remote": False},
        ],
        local_host="node-a",
        evidence_reader=lambda name, row: {
            "marker_id": None,
            "pid": None,
            "session": None,
            "heartbeat": None,
        },
        birth_reader=births,
    )

    # Assert
    assert records == {} and calls == 0


def test_stopped_status_never_reads_stale_active_birth() -> None:
    # Arrange
    calls = 0

    def births(ids: tuple[str, ...]) -> dict[str, dict]:
        nonlocal calls
        calls += 1
        return {"stale": _birth(engine_key="stale-selected")}

    # Act
    identity = status_runtime_identity(
        "alpha",
        AgentConfig(name="alpha", engine_key="current-spec"),
        False,
        registry_entry={"name": "alpha", "pid": 111},
        active_instances=[
            {"id": "stale", "name": "alpha", "host": "node-a", "pid": 111, "remote": False}
        ],
        local_host="node-a",
        birth_reader=births,
        evidence_reader=lambda name, row: {
            "marker_id": "stale",
            "pid": 111,
            "session": None,
            "heartbeat": None,
        },
    )

    # Assert
    assert identity["runtime_identity_source"] == "spec" and identity["engine"] == "current-spec" and calls == 0


def test_listener_liveness_is_determined_before_identity_authority() -> None:
    # Arrange
    class _StoppedRuntime:
        def is_running(self, config) -> bool:
            return False

    # Act
    running, status, liveness = _runtime_liveness(
        AgentConfig(name="alpha"), runtime_factory=lambda config: _StoppedRuntime()
    )

    # Assert
    assert running is False and status == "stopped" and liveness["verdict"] == "dead"
