"""``sac agents migrate-spec-uuids`` — CLI surface over the migration.

Real tmp git repos; AAA markers, one assertion per test.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.agent_group import agent_group
from scitex_agent_container.config._spec_uuid import ensure_spec_uuid
from tests.scitex_agent_container.config.test__spec_uuid_migrate import make_fleet

_OTHER = "0f0e0d0c-0b0a-4908-8706-050403020100"


def _run(*args: str):
    return CliRunner().invoke(agent_group, ["migrate-spec-uuids", *args])


def test_dry_run_prints_the_plan(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    stamped = ensure_spec_uuid(spec)
    # Act
    result = _run("--agents-dir", str(spec.parents[1]), "--dry-run")
    # Assert
    assert f"MIGRATED   agent-a  uuid={stamped}" in result.output


def test_dry_run_keeps_the_line(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    ensure_spec_uuid(spec)
    before = spec.read_bytes()
    # Act
    _run("--agents-dir", str(spec.parents[1]), "--dry-run")
    # Assert
    assert spec.read_bytes() == before


def test_apply_json_reports_migrated(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    ensure_spec_uuid(spec)
    # Act
    result = _run("agent-a", "--agents-dir", str(spec.parents[1]), "--json")
    # Assert
    assert json.loads(result.output)["results"][0]["action"] == "migrated"


def test_conflict_exits_non_zero(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    ensure_spec_uuid(spec)
    state = spec.parents[2] / "runtime" / "agent-a"
    state.mkdir(parents=True)
    (state / "spec_uuid").write_text(_OTHER + "\n")
    # Act
    result = _run("--agents-dir", str(spec.parents[1]))
    # Assert
    assert result.exit_code == 1


def test_unknown_agent_is_a_usage_error(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    # Act
    result = _run("nope", "--agents-dir", str(spec.parents[1]))
    # Assert
    assert result.exit_code == 2
