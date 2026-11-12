"""Fleet inspection names preserve worker protocol and JSON diagnostics."""

import json
import re

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg.fleet_group import fleet_group


@pytest.fixture
def inspection_environment(tmp_path, env_save_restore):
    """Owned HOME/config only; unresolved peer refuses before SSH."""
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    agents = home / ".scitex/agent-container/agents/demo"
    agents.mkdir(parents=True)
    (agents / "spec.yaml").write_text("owned-spec\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers:\n  unresolved: {resolve: {source: scitex-hpc}}\n")
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return agents / "spec.yaml"


@pytest.mark.parametrize(
    "canonical,legacy",
    [("validate-specs", "sync"), ("validate-checkouts", "sync-code")],
)
def test_fleet_worker_alias_keeps_exact_manifest(
    inspection_environment, canonical, legacy
):
    # Arrange
    runner = CliRunner()
    # Act
    current = runner.invoke(main, ["fleet", canonical, "--collect", "--json"])
    old = runner.invoke(main, ["fleet", legacy, "--collect", "--json"])
    # Assert
    assert (
        current.exit_code,
        old.exit_code,
        json.loads(current.stdout),
        canonical in old.stderr,
    ) == (0, 0, json.loads(old.stdout), True)


@pytest.mark.parametrize(
    "command,canonical,legacy",
    [
        ("validate-specs", "validate-specs", False),
        ("sync", "validate-specs", True),
        ("validate-checkouts", "validate-checkouts", False),
        ("sync-code", "validate-checkouts", True),
    ],
)
@pytest.mark.parametrize("prefix,suffix", [([], ["--json"]), (["--json"], [])])
def test_fleet_unresolved_json_refuses_without_partial_success(
    inspection_environment, command, canonical, legacy, prefix, suffix
):
    # Arrange
    args = [*prefix, "fleet", command, *suffix]
    # Act
    result = CliRunner().invoke(main, args)
    report = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        report["ok"],
        report["unreachable"][0]["peer"],
        canonical in result.stderr,
    ) == (2, False, "unresolved", legacy)


def test_fleet_help_shows_inspection_names_without_legacy_entries(
    inspection_environment,
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["--help"])
    # Assert
    assert (
        result.exit_code,
        bool(re.search(r"^  validate-specs\s", result.stdout, re.M)),
        bool(re.search(r"^  validate-checkouts\s", result.stdout, re.M)),
        bool(re.search(r"^  sync\s", result.stdout, re.M)),
        bool(re.search(r"^  sync-code\s", result.stdout, re.M)),
    ) == (0, True, True, False, False)
