"""Tests for the secret-scope axis — ``spec.secret_scope``.

Development agents run ``full`` (every pool-file-defined secret in the
profile env); insulated capsule solvers run ``capsule`` (declared
inference keys + delivery tokens only). The axis is DEFAULTED (absence
loads as full — the live corpus predates it) while an ILLEGAL value
fails loud naming the closed set.

Same harness as the residency-axis suite: REAL ``load_config`` against
REAL spec.yaml files on disk. STX-TQ002 AAA, STX-TQ007 one assert.
No mocks.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)
from scitex_agent_container.config._secret_scope_types import (
    CAPSULE,
    DEFAULT_AGENT_SECRET_SCOPE,
    FULL,
    is_known_secret_scope,
    resolve_spec_secret_scope,
    secret_scope_value_error,
)


def _write_spec(tmp_path, name: str, overrides: dict, *, kind: str = "Agent"):
    """Write ``<tmp_path>/<name>/spec.yaml`` and return its path."""
    spec = explicit_spec_defaults(kind)
    spec.update(overrides)
    spec["host"] = "${HOSTNAME}"
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": kind,
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return path


def _load_error(path):
    # Arrange/Act helper: return the load failure, or None when it loads.
    try:
        load_config(path)
    except Exception as exc:  # noqa: BLE001 - any refusal counts
        return exc
    return None


def test_absent_key_resolves_to_full():
    # Arrange
    spec: dict = {}
    # Act
    scope = resolve_spec_secret_scope(spec)
    # Assert
    assert scope == FULL


def test_capsule_resolves_to_capsule():
    # Arrange
    spec = {"secret_scope": "capsule"}
    # Act
    scope = resolve_spec_secret_scope(spec)
    # Assert
    assert scope == CAPSULE


def test_unknown_value_is_not_known():
    # Arrange
    value = "everything"
    # Act
    known = is_known_secret_scope(value)
    # Assert
    assert known is False


def test_value_error_names_the_valid_set():
    # Arrange
    spec = {"secret_scope": "everything"}
    # Act
    errors = secret_scope_value_error(spec)
    # Assert
    assert errors and "capsule" in errors[0] and "full" in errors[0]


def test_load_config_refuses_a_typo_loudly(tmp_path):
    # Arrange: a REAL spec on disk carrying the typo.
    path = _write_spec(tmp_path, "ag-typo", {"secret_scope": "fulll"})
    # Act
    exc = _load_error(path)
    # Assert
    assert exc is not None and "secret_scope" in str(exc)


def test_compiled_spec_carries_declared_capsule(tmp_path):
    # Arrange
    path = _write_spec(tmp_path, "ag-capsule", {"secret_scope": "capsule"})
    # Act
    config = load_config(path)
    # Assert
    assert config.secret_scope == CAPSULE


def test_spec_without_the_field_loads_as_full(tmp_path):
    # Arrange: the live-fleet shape — a spec that predates the axis.
    path = _write_spec(tmp_path, "ag-legacy", {})
    # Act
    config = load_config(path)
    # Assert
    assert config.secret_scope == DEFAULT_AGENT_SECRET_SCOPE
