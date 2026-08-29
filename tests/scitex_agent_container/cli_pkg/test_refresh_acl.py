"""``sac agents refresh-acl`` — re-publish fleet ACL/group policy from
specs without an agent relaunch.

Real on-disk fixtures only (no mocks): a tmp fleet registry of v3
``spec.yaml`` files + a tmp ``state.db``, both wired via env overrides
(``SCITEX_AGENT_CONTAINER_STATE_DB`` and
``SCITEX_AGENT_CONTAINER_AGENTS_DIR``) in yield-based fixtures — the
pattern from ``tests/scitex_agent_container/_listen/test__acl_group.py``.
The command is exercised end-to-end through its real Click entrypoint
against those real on-disk files.

AAA (each marker on its own line), one assertion per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml as _yaml
from click.testing import CliRunner

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    read_comms_policy,
    record_comms_policy,
)
from scitex_agent_container.cli_pkg import refresh_acl as refresh_acl_mod
from scitex_agent_container.cli_pkg.refresh_acl import refresh_acl


@pytest.fixture
def db_path(tmp_path: Path):
    # Arrange — isolated on-disk state.db via the env override.
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


def _required_scaffold() -> dict:
    """Fully-explicit scaffold (red-start ruling 2026-07-21)."""
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    return explicit_spec(
        {
            "host": "${HOSTNAME}",
            "runtime": "apptainer",
            "claude": {"model": "claude-opus-4-8[1m]"},
            "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        }
    )


def _write_spec(registry: Path, name: str, labels: dict | None) -> Path:
    """Write a dir-as-SSoT v3 ``<name>/spec.yaml`` under ``registry``."""
    agent_dir = registry / name
    agent_dir.mkdir(parents=True)
    spec_body = _required_scaffold()
    spec_body["workdir"] = f"~/.scitex/agent-container/runtime/agents/{name}"
    metadata = {"labels": labels} if labels is not None else {}
    spec_path = agent_dir / "spec.yaml"
    spec_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": metadata,
                "spec": spec_body,
            }
        )
    )
    return spec_path


@pytest.fixture
def registry(tmp_path: Path):
    # Arrange — a tmp fleet registry the command globs directly, wired via
    # the SCITEX_AGENT_CONTAINER_AGENTS_DIR env override (yield-based; the
    # value is popped/restored on teardown).
    reg = tmp_path / "agents"
    reg.mkdir()
    env_key = refresh_acl_mod._REGISTRY_ENV
    saved = os.environ.get(env_key)
    os.environ[env_key] = str(reg)
    try:
        yield reg
    finally:
        if saved is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved


def test_refresh_writes_resolved_group_for_stale_agent(pg_schema: str, db_path, registry):
    """A spec with groups:[researcher] whose persisted group is stale →
    after the command read_comms_policy returns ``researcher``."""
    # Arrange — spec says researcher; DB still holds the stale developer.
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="developer")
    # Act
    CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert read_comms_policy(name="alice")["group_name"] == "researcher"


def test_diff_output_shows_the_change(pg_schema: str, db_path, registry):
    """The printed per-agent diff shows old -> new for a changed group."""
    # Arrange
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="developer")
    # Act
    result = CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert "alice: developer -> researcher" in result.output


def test_unchanged_agent_marked_unchanged(pg_schema: str, db_path, registry):
    """An agent already at its resolved group prints ``(unchanged)``."""
    # Arrange
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="researcher")
    # Act
    result = CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert "(unchanged)" in result.output


def test_dry_run_does_not_mutate_the_db(pg_schema: str, db_path, registry):
    """--dry-run shows the diff WITHOUT writing — DB stays stale."""
    # Arrange
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="developer")
    # Act
    CliRunner().invoke(refresh_acl, ["--dry-run"], catch_exceptions=False)
    # Assert
    assert read_comms_policy(name="alice")["group_name"] == "developer"


def test_dry_run_still_previews_the_new_group(pg_schema: str, db_path, registry):
    """--dry-run previews the would-be group in the diff output."""
    # Arrange
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="developer")
    # Act
    result = CliRunner().invoke(refresh_acl, ["--dry-run"], catch_exceptions=False)
    # Assert
    assert "alice: developer -> researcher" in result.output


def test_underscore_dirs_are_skipped(pg_schema: str, db_path, registry):
    """``_shared`` / ``_template_*`` dirs are NOT treated as fleet agents."""
    # Arrange — a real agent plus two underscore scaffolding dirs.
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    _write_spec(registry, "_shared", {"groups": ["researcher"]})
    _write_spec(registry, "_template_dev", {"groups": ["developer"]})
    # Act
    result = CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert "_shared" not in result.output and "_template_dev" not in result.output


def test_malformed_spec_reported_with_nonzero_exit(pg_schema: str, db_path, registry):
    """A malformed spec is reported + non-zero exit, but the others still
    refresh."""
    # Arrange — one good agent, one spec.yaml that fails validation.
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    bad_dir = registry / "broken"
    bad_dir.mkdir()
    (bad_dir / "spec.yaml").write_text("this: is: not: a: valid: v3 spec\n")
    # Act
    result = CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert result.exit_code == 1


def test_malformed_spec_does_not_abort_the_others(pg_schema: str, db_path, registry):
    """The good agent is still refreshed despite the sibling failure."""
    # Arrange
    _write_spec(registry, "alice", {"groups": ["researcher"]})
    record_comms_policy(name="alice", group_name="developer")
    bad_dir = registry / "broken"
    bad_dir.mkdir()
    (bad_dir / "spec.yaml").write_text("this: is: not: a: valid: v3 spec\n")
    # Act
    CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    # Assert
    assert read_comms_policy(name="alice")["group_name"] == "researcher"


def test_missing_registry_dir_fails_loud(db_path, tmp_path):
    """A missing registry dir is an actionable non-zero failure."""
    # Arrange — point the env override at a path that does not exist.
    missing = tmp_path / "nope" / "agents"
    env_key = refresh_acl_mod._REGISTRY_ENV
    saved = os.environ.get(env_key)
    os.environ[env_key] = str(missing)
    try:
        # Act
        result = CliRunner().invoke(refresh_acl, [], catch_exceptions=False)
    finally:
        if saved is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved
    # Assert
    assert result.exit_code != 0
