"""``sac agents reconcile`` — the CLI surface of the fleet enforcer.

Real ``CliRunner`` against the real click command; the engine underneath is
covered by ``tests/scitex_agent_container/_reconcile/``. These tests pin the
things only the CLI owns: that dry-run is the DEFAULT (the single most
important safety property of this verb — a wrong default here restarts the
fleet by accident), the flag contract, and the exit codes a cron reads.

The pass is redirected at a REAL empty tmp fleet registry via
``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` so the command can never touch the
operator's live fleet from a test run. No mocks.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_reconcile import reconcile


@pytest.fixture(autouse=True)
def empty_fleet(tmp_path: Path):
    # Arrange — point the verb at an EMPTY tmp registry (env override, the
    # same seam `sac agents refresh-acl` honours) so no test can ever read
    # or restart the operator's real agents. Explicit save/restore.
    reg = tmp_path / "agents"
    reg.mkdir()
    keys = {
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(reg),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": str(tmp_path / "runtime"),
        "SCITEX_TODO_TASKS_YAML_SHARED": str(tmp_path / "tasks.yaml"),
    }
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(keys)
    try:
        yield reg
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def default_run() -> object:
    # Arrange + Act — the bare verb, no flags at all.
    return CliRunner().invoke(reconcile, [])


def test_bare_invocation_is_a_dry_run(default_run):
    # Arrange — THE safety property: no flags must never mutate the fleet.
    # Act
    # Assert
    assert "dry-run" in default_run.output


def test_bare_invocation_on_clean_fleet_exits_zero(default_run):
    # Arrange — an empty registry has nothing down.
    # Act
    # Assert
    assert default_run.exit_code == 0


def test_apply_and_dry_run_together_are_refused():
    # Arrange — contradictory flags must fail loud, never pick one silently.
    # Act
    result = CliRunner().invoke(reconcile, ["--apply", "--dry-run"])
    # Assert
    assert result.exit_code == 2


def test_apply_and_dry_run_conflict_explains_the_default():
    # Arrange
    # Act
    result = CliRunner().invoke(reconcile, ["--apply", "--dry-run"])
    # Assert
    assert "Dry-run is the DEFAULT" in result.output


def test_json_mode_emits_parseable_output():
    # Arrange — the scheduled form and any tooling read this.
    # Act
    result = CliRunner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.output)["mode"] == "dry-run"


def test_json_mode_reports_the_exit_code():
    # Arrange — the envelope carries the verdict for a non-tty consumer.
    # Act
    result = CliRunner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.output)["exit_code"] == 0


def test_json_mode_lists_agents():
    # Arrange — an empty fleet reports an empty list, not a missing key.
    # Act
    result = CliRunner().invoke(reconcile, ["--json"])
    # Assert
    assert json.loads(result.output)["agents"] == []


def test_help_documents_the_dry_run_default():
    # Arrange — an operator must learn the default from --help, not by
    # restarting the fleet.
    # Act
    result = CliRunner().invoke(reconcile, ["--help"])
    # Assert
    assert "Dry-run by default" in result.output


def test_help_documents_the_apply_flag():
    # Arrange
    # Act
    result = CliRunner().invoke(reconcile, ["--help"])
    # Assert
    assert "--apply" in result.output


def test_limit_flag_is_accepted():
    # Arrange — the global per-pass cap must be operator-tunable.
    # Act
    result = CliRunner().invoke(reconcile, ["--limit", "3", "--json"])
    # Assert
    assert result.exit_code == 0


def test_verb_is_registered_on_the_agents_group():
    # Arrange — a verb nobody can invoke is a verb that does not exist.
    from scitex_agent_container.cli_pkg.agent_group import agent_group

    # Act
    names = sorted(agent_group.commands)
    # Assert
    assert "reconcile" in names
