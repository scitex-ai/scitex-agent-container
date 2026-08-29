"""Tests for ``sac db`` group console output.

Coverage closure for ``scitex_agent_container.cli_pkg.db_group``.
Targets the uncovered rich-console paths of ``db migrate`` and
``db clean``.

The ``db show`` / ``db query`` / ``db export`` / ``db import`` cases that
made up the bulk of this file were deleted on 2026-08-29 with the verbs
themselves — the SQLite read surface went, so there is nothing left for
them to exercise.

PA-306 conventions:

* No mocks. Real ``CliRunner`` against the real click commands. The
  ``instances`` rows the verbs read live in the throwaway PostgreSQL
  store the ``pg_schema`` fixture provides.
* AAA structure, one assertion per test, 3+ word descriptive names.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location pinned via env, reloaded on teardown."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)

    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# db migrate — default registry_dir resolution + console branch
# (lines 168, 178)
# ---------------------------------------------------------------------------


def test_db_migrate_resolves_registry_dir_from_env_variable(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "legacy-reg"
    reg.mkdir()
    (reg / "alpha.json").write_text(
        json.dumps(
            {
                "name": "alpha",
                "config": "/dev/null/alpha.yaml",
                "pid": 1,
                "started_at": "2026-05-05T03:29:41Z",
                "screen": "alpha",
            }
        )
    )
    key = "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
    saved = os.environ.get(key)
    os.environ[key] = str(reg)
    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    try:
        # Act
        result = runner.invoke(db_migrate, ["--host", "h", "--json"])
        body = json.loads(result.stdout)
        # Assert
        assert body == {"registry_dir": str(reg), "imported": 1, "skipped": 0}
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_db_migrate_console_output_reports_imported_count(
    db_path: Path, tmp_path: Path
):
    # Arrange
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "x.json").write_text(
        json.dumps(
            {
                "name": "x",
                "config": "/dev/null/x.yaml",
                "pid": 2,
                "started_at": "2026-05-05T03:29:42Z",
                "screen": "x",
            }
        )
    )
    from scitex_agent_container.cli_pkg.db_group import db_migrate

    runner = CliRunner()
    # Act
    result = runner.invoke(db_migrate, ["--registry-dir", str(reg), "--host", "h"])
    # Assert
    assert "imported=1" in result.output


# ---------------------------------------------------------------------------
# db clean — non-JSON console branch (lines 240-245)
# ---------------------------------------------------------------------------


@pytest.fixture
def dead_pid_environment():
    """Pin host + suppress proc-btime so a fake pid reaps as crashed."""
    from scitex_agent_container._state import state_db

    saved_host = os.environ.get("SAC_HOST")
    saved_btime = state_db._proc_btime
    os.environ["SAC_HOST"] = "test-host"
    state_db._proc_btime = lambda: None  # type: ignore[assignment]
    try:
        yield
    finally:
        state_db._proc_btime = saved_btime  # type: ignore[assignment]
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host


def test_db_clean_console_output_emits_swept_label_header(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, [])
    # Assert
    assert "swept=" in result.output


def test_db_clean_dry_run_console_uses_would_sweep_label(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, ["--dry-run"])
    # Assert
    assert "would-sweep=" in result.output


def test_db_clean_console_lists_crashed_counter_when_nonzero(
    db_path: Path, dead_pid_environment
):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container.cli_pkg.db_group import db_clean

    record_instance_start("dead", pid=999_999_999, host="test-host")
    runner = CliRunner()
    # Act
    result = runner.invoke(db_clean, [])
    # Assert
    assert "crashed" in result.output
