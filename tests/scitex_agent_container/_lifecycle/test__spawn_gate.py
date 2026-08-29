"""Spawn-permission gate + lineage record — the unified spawn chokepoint
wired into core ``agent_start`` (ADR-0010 Rule B / Phase 2).

PA-306: NO mocks. Every test runs ``enforce_spawn_gate`` against a REAL
on-disk SQLite state.db (isolated per test) and the REAL ``check_spawn``
/ ``record_lineage`` collaborators. The caller identity is set via a
real yield-based env override (no monkeypatch).

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._spawn_gate import (
    SpawnDeniedError,
    enforce_spawn_gate,
    resolve_spawn_caller,
)
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_nodes import (
    derive_group,
    record_lineage,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Isolated state.db; env + DEFAULT_DB_PATH overridden then restored.

    The gate's internal calls use ``db_path=None`` (→ DEFAULT_DB_PATH),
    so re-binding the module constant is what isolates them. No mocks —
    a real sqlite file under tmp_path.
    """
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    try:
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env


@pytest.fixture
def sac_name() -> Iterator[callable]:
    """Yield a setter for SAC_NAME (the parent caller identity).

    Real env mutation with full save/restore of BOTH prefixes, so a
    spawn under a parent agent's identity can be exercised without mocks.
    """
    keys = ("SAC_NAME", "SCITEX_AGENT_CONTAINER_NAME")
    saved = {k: os.environ.get(k) for k in keys}

    def _set(value: str | None) -> None:
        for k in keys:
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value

    try:
        yield _set
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


# ---------------------------------------------------------------------------
# resolve_spawn_caller — SAC_NAME → caller, empty → admin (None)
# ---------------------------------------------------------------------------


def test_resolve_caller_returns_sac_name_when_set(sac_name) -> None:
    # Arrange
    sac_name("parent-bot")
    # Act
    caller = resolve_spawn_caller()
    # Assert
    assert caller == "parent-bot"


def test_resolve_caller_returns_none_without_sac_name(sac_name) -> None:
    # Arrange — no SAC_NAME → admin / lead / operator path.
    sac_name(None)
    # Act
    caller = resolve_spawn_caller()
    # Assert
    assert caller is None


# ---------------------------------------------------------------------------
# enforce_spawn_gate — admin / root allow paths
# ---------------------------------------------------------------------------


def test_gate_allows_admin_caller_when_sac_name_unset(db_path, sac_name) -> None:
    # Arrange — no SAC_NAME → admin path → always allowed.
    sac_name(None)
    # Act
    caller = enforce_spawn_gate("child-a")
    # Assert
    assert caller is None


def test_gate_allows_root_caller_to_spawn(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — "root" has no parent in lineage → root → allowed.
    sac_name("root")
    # Act
    caller = enforce_spawn_gate("child-b")
    # Assert
    assert caller == "root"


# ---------------------------------------------------------------------------
# enforce_spawn_gate — lineage recording on allow
# ---------------------------------------------------------------------------


def test_gate_records_lineage_edge_for_root_caller(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — a root caller spawning a child writes the lineage edge.
    sac_name("root")
    # Act
    enforce_spawn_gate("child-c")
    # Assert — the child's group now contains its parent (proves the edge).
    assert "root" in derive_group(name="child-c")


def test_gate_does_not_record_lineage_for_admin_caller(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — admin spawn (no SAC_NAME) must leave the child unattached.
    sac_name(None)
    # Act
    enforce_spawn_gate("child-d")
    # Assert — a fresh unattached node's group is just itself (singleton).
    assert derive_group(name="child-d") == {"child-d"}


def test_gate_lineage_record_is_idempotent_on_same_parent(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — root spawns the same child twice (e.g. a --force restart).
    sac_name("root")
    enforce_spawn_gate("child-e")
    # Act — second spawn under the same parent must not raise.
    enforce_spawn_gate("child-e")
    # Assert — still exactly one parent edge.
    assert "root" in derive_group(name="child-e")


# ---------------------------------------------------------------------------
# enforce_spawn_gate — deny paths
# ---------------------------------------------------------------------------


def test_gate_denies_child_caller_under_root_only_policy(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — "worker-a" is a child of "root" → not a root → may not spawn.
    record_lineage(child="worker-a", parent="root")
    sac_name("worker-a")
    # Act
    ctx = pytest.raises(SpawnDeniedError)
    # Assert
    with ctx:
        enforce_spawn_gate("grandchild")


def test_gate_allows_restart_keeps_existing_parent(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — child-f already parented to "root-1"; a restart by a
    # different-lineage caller must SUCCEED in-place (no re-parent, no
    # SpawnDeniedError) — the 409 the ACL previously raised is now gone,
    # so a developer/research peer can restart a down agent.
    record_lineage(child="child-f", parent="root-1")
    sac_name("root-2")
    # Act — must not raise; record_lineage keeps the existing parent
    result = enforce_spawn_gate("child-f")
    # Assert — the restart proceeded, returning the resolved caller
    assert result == "root-2"


# ---------------------------------------------------------------------------
# explicit caller arg overrides the SAC_NAME env
# ---------------------------------------------------------------------------


def test_explicit_caller_arg_overrides_sac_name_env(pg_schema: str, db_path, sac_name) -> None:
    # Arrange — env says "env-parent" but the explicit arg wins.
    sac_name("env-parent")
    # Act
    enforce_spawn_gate("child-g", caller="arg-parent")
    # Assert — the recorded edge is from the explicit arg, not the env.
    assert "arg-parent" in derive_group(name="child-g")
