#!/usr/bin/env python3
"""RED/GREEN for the instances-verify defect: a bare count with no name on it.

MEASURED on the fleet primary the night ``instances`` moved to PostgreSQL:
``sac_instances_rows`` — a physical table declared by ``_store_plugin``'s
classification namespace, not by this store — already existed, EMPTY,
alongside the ``instances_rows`` this store actually opens. See
``scripts/_pg_relation_candidates.py`` and the module docstring of
``scripts/migrate_instances_to_postgres.py`` for the full incident.

RED, DEMONSTRATED DIRECTLY: :func:`test_the_old_bare_count_would_have_hidden_
the_sibling` runs the OLD query verbatim — copied from the pre-fix
``_verify_as_consumer``, not reconstructed — against a fixture shaped like
the measured incident, and shows it silently prints an unlabelled ``0``
while five real rows sit one name away. Everything below exercises the FIXED
code against the same and other fixtures.

NO MOCKS (PA-306). A real throwaway PostgreSQL schema via the shared
``pg_schema`` fixture (``tests._store_isolation``), no monkeypatch anywhere.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate_instances_to_postgres.py"


@contextlib.contextmanager
def _loaded() -> Iterator[Any]:
    """Import the script by path — it has no importable package home."""
    if not SCRIPT.exists():
        pytest.skip(f"{SCRIPT.name} not present in this checkout")
    spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved


def _candidates_module() -> Any:
    saved = list(sys.path)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        return importlib.import_module("_pg_relation_candidates")
    finally:
        sys.path[:] = saved


def _raw_conn() -> Any:
    """A connection scoped to the throwaway schema ``pg_schema`` created."""
    import psycopg

    return psycopg.connect(os.environ["SCITEX_STORE_DSN"], autocommit=True)


def _make_table(conn: Any, name: str, n_rows: int) -> None:
    conn.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)')
    if n_rows:
        conn.execute(
            f'INSERT INTO "{name}" (id) SELECT generate_series(1, %s)', (n_rows,)
        )


# ---------------------------------------------------------------------------
# RED — the pre-fix behaviour, run verbatim against the measured shape.
# ---------------------------------------------------------------------------


def test_the_old_bare_count_would_have_hidden_the_sibling(pg_schema: str) -> None:
    """THE OLD CODE, its exact statement, against the exact measured shape."""
    # Arrange
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 0)
        _make_table(conn, "sac_instances_rows", 5)
        # Act — copied verbatim from the pre-fix `_verify_as_consumer`
        n = conn.execute("SELECT COUNT(*) FROM instances_rows").fetchone()[0]
    # Assert — a plausible, unlabelled zero; 5 real rows sit one name away
    assert n == 0


# ---------------------------------------------------------------------------
# `_pg_relation_candidates` — the enumeration primitive, in isolation.
# ---------------------------------------------------------------------------


def test_candidate_relations_finds_both_bare_and_prefixed(pg_schema: str) -> None:
    # Arrange
    relations = _candidates_module()
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 3)
        _make_table(conn, "sac_instances_rows", 0)
        # Act
        found = {
            c["table"]
            for c in relations.candidate_relations(conn, table="instances_rows")
        }
    # Assert
    assert found == {"instances_rows", "sac_instances_rows"}


def test_candidate_relations_reports_the_right_count_per_name(pg_schema: str) -> None:
    # Arrange
    relations = _candidates_module()
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 3)
        _make_table(conn, "sac_instances_rows", 0)
        # Act
        counts = {
            c["table"]: c["count"]
            for c in relations.candidate_relations(conn, table="instances_rows")
        }
    # Assert
    assert counts == {"instances_rows": 3, "sac_instances_rows": 0}


def test_find_authoritative_matches_the_bare_name(pg_schema: str) -> None:
    # Arrange
    relations = _candidates_module()
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 1)
        _make_table(conn, "sac_instances_rows", 1)
        candidates = relations.candidate_relations(conn, table="instances_rows")
        # Act
        authoritative = relations.find_authoritative(
            conn, candidates, table="instances_rows"
        )
    # Assert
    assert authoritative["table"] == "instances_rows"


def test_find_authoritative_is_none_when_nothing_matches(pg_schema: str) -> None:
    """NEGATIVE CONTROL — an empty schema has no answer, not a wrong one."""
    # Arrange
    relations = _candidates_module()
    with _raw_conn() as conn:
        # Act
        authoritative = relations.find_authoritative(conn, [], table="instances_rows")
    # Assert
    assert authoritative is None


def test_ambiguous_true_when_authoritative_empty_and_sibling_holds_rows() -> None:
    """THE MEASURED SHAPE — the honest answer is 'ambiguous', not '0'."""
    # Arrange
    relations = _candidates_module()
    authoritative = {"count": 0}
    candidates = [authoritative, {"count": 5}]
    # Act
    result = relations.ambiguous(authoritative, candidates)
    # Assert
    assert result is True


def test_ambiguous_true_when_two_candidates_both_hold_rows() -> None:
    # Arrange
    relations = _candidates_module()
    authoritative = {"count": 3}
    candidates = [authoritative, {"count": 5}]
    # Act
    result = relations.ambiguous(authoritative, candidates)
    # Assert
    assert result is True


def test_ambiguous_false_when_only_the_authoritative_holds_rows() -> None:
    """NEGATIVE CONTROL — the ordinary, unambiguous, successful shape."""
    # Arrange
    relations = _candidates_module()
    authoritative = {"count": 3}
    candidates = [authoritative, {"count": 0}]
    # Act
    result = relations.ambiguous(authoritative, candidates)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# GREEN — `_verify_as_consumer`, fixed, against the same fixtures.
# ---------------------------------------------------------------------------


def test_verify_as_consumer_refuses_the_measured_data_loss_shape(
    pg_schema: str,
) -> None:
    """GREEN — catches exactly what the RED test above could not."""
    # Arrange
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 0)
        _make_table(conn, "sac_instances_rows", 5)
    log: list[str] = []
    # Act
    with _loaded() as module:
        ok = module._verify_as_consumer("throwaway", log.append)
    # Assert
    assert ok is False


def test_verify_as_consumer_names_both_relations_in_the_refusal(
    pg_schema: str,
) -> None:
    # Arrange
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 0)
        _make_table(conn, "sac_instances_rows", 5)
    log: list[str] = []
    # Act
    with _loaded() as module:
        module._verify_as_consumer("throwaway", log.append)
    out = "\n".join(log)
    # Assert
    assert "instances_rows" in out and "sac_instances_rows" in out


def test_verify_as_consumer_passes_the_ordinary_unambiguous_shape(
    pg_schema: str,
) -> None:
    """NEGATIVE CONTROL — a real, unambiguous migration must still verify."""
    # Arrange
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 7)
    log: list[str] = []
    # Act
    with _loaded() as module:
        ok = module._verify_as_consumer("throwaway", log.append)
    # Assert
    assert ok is True


def test_verify_as_consumer_prints_the_fully_qualified_name(pg_schema: str) -> None:
    """A number with no relation name attached is the thing being fixed."""
    # Arrange
    with _raw_conn() as conn:
        _make_table(conn, "instances_rows", 7)
    log: list[str] = []
    # Act
    with _loaded() as module:
        module._verify_as_consumer("throwaway", log.append)
    out = "\n".join(log)
    # Assert
    assert f"{pg_schema}.instances_rows" in out


def test_verify_as_consumer_fails_when_no_relation_exists_at_all(
    pg_schema: str,
) -> None:
    """No candidate anywhere is a FAILURE, not a false 'nothing migrated'."""
    # Arrange — pg_schema alone: a fresh, empty schema, nothing created
    log: list[str] = []
    # Act
    with _loaded() as module:
        ok = module._verify_as_consumer("throwaway", log.append)
    # Assert
    assert ok is False


def test_verify_as_consumer_skips_cleanly_when_dsn_is_unset() -> None:
    """Unrelated to the fix; pinned so it cannot silently start FAILING."""
    # Arrange
    saved = os.environ.pop("SCITEX_STORE_DSN", None)
    log: list[str] = []
    try:
        with _loaded() as module:
            # Act
            ok = module._verify_as_consumer("throwaway", log.append)
    finally:
        if saved is not None:
            os.environ["SCITEX_STORE_DSN"] = saved
    # Assert
    assert ok is True
