"""Tests for ``sac agents new`` — scaffold a fresh v3 spec.yaml.

Card sac-fresh-agent-specs (2026-06-13). Authoring policy is "fresh
template, not in-place repair": the operator runs ``sac agents new
<name>`` and gets a v3-clean spec.yaml + to_home/ skeleton next to it,
ready to edit. The validator must accept the output as-is.

Discipline: AAA markers each on their own line; one literal ``assert``
per test; real filesystem fixtures (``tmp_path``), no mocks.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._new import new as new_cmd
from scitex_agent_container.config._validation import validate_config


def test_new_writes_spec_yaml_at_target(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["my-agent", "--base-dir", str(base)])
    # Assert
    assert (base / "my-agent" / "spec.yaml").is_file()


def test_new_minimal_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        new_cmd, ["fresh-agent", "--base-dir", str(base), "--template", "minimal"]
    )
    errors = validate_config(base / "fresh-agent" / "spec.yaml")
    # Assert — fresh template must satisfy the live validator (zero errors).
    assert errors == []


def test_new_full_template_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        new_cmd, ["full-fresh", "--base-dir", str(base), "--template", "full"]
    )
    errors = validate_config(base / "full-fresh" / "spec.yaml")
    # Assert
    assert errors == []


def test_new_default_template_is_minimal(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(new_cmd, ["defaulty", "--base-dir", str(base)])
    # Act — parse the spec YAML to inspect the rendered config keys, NOT
    # the prose docstring (the comment legitimately MENTIONS the field
    # name as an "add this if you need it" pointer).
    parsed = yaml.safe_load((base / "defaulty" / "spec.yaml").read_text())
    # Assert — minimal template omits startup_prompts; the full template ships it.
    assert "startup_prompts" not in parsed.get("spec", {})


def test_new_creates_to_home_skeleton(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["agent-x", "--base-dir", str(base)])
    # Assert — to_home/ exists as a sibling of spec.yaml so the runtime
    # auto-discovers it (spec-reference §to_home).
    assert (base / "agent-x" / "to_home").is_dir()


def test_new_refuses_to_overwrite_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe").mkdir(parents=True)
    (base / "dupe" / "spec.yaml").write_text("# pre-existing\n")
    # Act
    result = runner.invoke(new_cmd, ["dupe", "--base-dir", str(base)])
    # Assert — non-zero exit so accidental clobber is impossible without --force.
    assert result.exit_code != 0


def test_new_force_overwrites_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe2").mkdir(parents=True)
    (base / "dupe2" / "spec.yaml").write_text("# stale\n")
    # Act
    runner.invoke(new_cmd, ["dupe2", "--base-dir", str(base), "--force"])
    text = (base / "dupe2" / "spec.yaml").read_text()
    # Assert — fresh template replaces the stale stub (apiVersion line is canonical).
    assert "scitex-agent-container/v3" in text


def test_new_rejects_unknown_template(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(
        new_cmd, ["bad", "--base-dir", str(base), "--template", "nope"]
    )
    # Assert — Click's Choice raises UsageError (exit code 2).
    assert result.exit_code != 0


def test_new_emits_canonical_apiversion_header(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["headered", "--base-dir", str(base)])
    first_lines = (base / "headered" / "spec.yaml").read_text().splitlines()[:10]
    # Assert — apiVersion appears in the file head (no buried boilerplate).
    assert any("apiVersion: scitex-agent-container/v3" in line for line in first_lines)


def test_new_template_kind_is_agent_not_agentproxy(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(new_cmd, ["kindly", "--base-dir", str(base)])
    text = (base / "kindly" / "spec.yaml").read_text()
    # Assert — default scaffold is the common case (SDK runner).
    assert "kind: Agent" in text


def test_new_rejects_invalid_agent_name(tmp_path: Path) -> None:
    # Arrange — names with slashes would write outside the base dir.
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(new_cmd, ["bad/name", "--base-dir", str(base)])
    # Assert
    assert result.exit_code != 0
