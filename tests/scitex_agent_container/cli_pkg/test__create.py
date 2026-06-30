"""Tests for ``sac agents create`` — stamp a proven-shape agent spec.

Card sac-templated-agent-create (2026-06-25). Folds the retired
``new_agent_spec.sh`` / ``gen_ecosystem_dev_specs.sh`` stampers into one
CLI. The developer/scientist skeletons must render to a v3-clean spec
that the live validator accepts, with the editable-install and Telegram
blocks toggled by auto-detection.

Discipline: AAA markers each on their own line; one literal ``assert``
per test; real filesystem fixtures (``tmp_path``), no mocks.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._create import create as create_cmd
from scitex_agent_container.cli_pkg._create import render_spec
from scitex_agent_container.config._validation import validate_config


def _spec(base: Path, name: str) -> Path:
    return base / name / "spec.yaml"


def test_create_writes_spec_yaml_at_target(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["dev-x", "--template", "developer", "--base-dir", str(base)])
    # Assert
    assert _spec(base, "dev-x").is_file()


def test_create_emits_empty_to_home_mcp_json(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        create_cmd, ["dev-mcp", "--template", "developer", "--base-dir", str(base)]
    )
    # Assert — the proven spec shape includes a per-agent to_home/.mcp.json.
    assert (base / "dev-mcp" / "to_home" / ".mcp.json").is_file()


def test_create_to_home_mcp_json_has_empty_servers(tmp_path: Path) -> None:
    # Arrange
    import json

    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        create_cmd, ["dev-mcp2", "--template", "developer", "--base-dir", str(base)]
    )
    doc = json.loads((base / "dev-mcp2" / "to_home" / ".mcp.json").read_text())
    # Assert — empty mcpServers (proven figrecipe shape), not a dropped file.
    assert doc == {"mcpServers": {}}


def test_create_does_not_clobber_existing_mcp_json(tmp_path: Path) -> None:
    # Arrange — a pre-existing custom .mcp.json must survive (create-if-absent).
    runner = CliRunner()
    base = tmp_path / "agents"
    mcp = base / "dev-keep" / "to_home" / ".mcp.json"
    mcp.parent.mkdir(parents=True, exist_ok=True)
    mcp.write_text('{"mcpServers": {"custom": {}}}')
    # Act
    runner.invoke(
        create_cmd,
        ["dev-keep", "--template", "developer", "--base-dir", str(base), "--force"],
    )
    # Assert — the custom file is untouched.
    assert "custom" in mcp.read_text()


def test_create_developer_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["dev-val", "--template", "developer", "--base-dir", str(base)])
    errors = validate_config(_spec(base, "dev-val"))
    # Assert — rendered developer spec must satisfy the live validator.
    assert errors == []


def test_create_scientist_passes_v3_validator(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(create_cmd, ["sci-val", "--template", "scientist", "--base-dir", str(base)])
    errors = validate_config(_spec(base, "sci-val"))
    # Assert
    assert errors == []


def test_create_developer_group_label(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, ["dev-grp", "--template", "developer", "--base-dir", str(base)])
    # Act
    parsed = yaml.safe_load(_spec(base, "dev-grp").read_text())
    # Assert — developer template defaults to the developer group.
    assert parsed["metadata"]["labels"]["groups"] == ["developer"]


def test_create_developer_purpose_suffix(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, ["dev-pur", "--template", "developer", "--base-dir", str(base)])
    # Act
    parsed = yaml.safe_load(_spec(base, "dev-pur").read_text())
    # Assert — developer purpose is the maintainer suffix.
    assert parsed["metadata"]["labels"]["purpose"] == "dev-pur-maintainer"


def test_create_scientist_group_label(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, ["sci-grp", "--template", "scientist", "--base-dir", str(base)])
    # Act
    parsed = yaml.safe_load(_spec(base, "sci-grp").read_text())
    # Assert — scientist template defaults to the scientist group.
    assert parsed["metadata"]["labels"]["groups"] == ["scientist"]


def test_create_scientist_purpose_suffix(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(create_cmd, ["sci-pur", "--template", "scientist", "--base-dir", str(base)])
    # Act
    parsed = yaml.safe_load(_spec(base, "sci-pur").read_text())
    # Assert — scientist purpose is the research suffix.
    assert parsed["metadata"]["labels"]["purpose"] == "sci-pur-research"


def test_create_group_override(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    runner.invoke(
        create_cmd,
        ["grp-ovr", "--template", "developer", "--group", "platform", "--base-dir", str(base)],
    )
    # Act
    parsed = yaml.safe_load(_spec(base, "grp-ovr").read_text())
    # Assert — explicit --group wins over the template default.
    assert parsed["metadata"]["labels"]["groups"] == ["platform"]


def test_create_install_block_present_when_package(tmp_path: Path) -> None:
    # Arrange — a workdir that ships a package triggers the editable install.
    runner = CliRunner()
    base = tmp_path / "agents"
    workdir = tmp_path / "pkg"
    workdir.mkdir()
    (workdir / "pyproject.toml").write_text("[project]\nname='x'\n")
    # Act
    runner.invoke(
        create_cmd,
        ["pkg-agent", "--template", "developer", "--workdir", str(workdir), "--base-dir", str(base)],
    )
    text = _spec(base, "pkg-agent").read_text()
    # Assert — editable install command is emitted for a packaged workdir.
    assert "uv pip install --python /uvwork/venv-agent/bin/python -e ." in text


def test_create_install_block_absent_when_no_package(tmp_path: Path) -> None:
    # Arrange — an empty workdir ships no package.
    runner = CliRunner()
    base = tmp_path / "agents"
    workdir = tmp_path / "nopkg"
    workdir.mkdir()
    # Act
    runner.invoke(
        create_cmd,
        ["nopkg-agent", "--template", "developer", "--workdir", str(workdir), "--base-dir", str(base)],
    )
    text = _spec(base, "nopkg-agent").read_text()
    # Assert — no editable install line when the workdir has no package.
    assert "uv pip install --python /uvwork/venv-agent/bin/python -e ." not in text


def test_create_telegram_block_present_with_token(tmp_path: Path) -> None:
    # Arrange — an existing token file wires the per-agent bot.
    runner = CliRunner()
    base = tmp_path / "agents"
    token = tmp_path / "bot-token.txt"
    token.write_text("123:abc\n")
    # Act
    runner.invoke(
        create_cmd,
        ["tg-agent", "--template", "scientist", "--telegram-token", str(token), "--base-dir", str(base)],
    )
    parsed = yaml.safe_load(_spec(base, "tg-agent").read_text())
    # Assert — the telegrammer channel is wired when a token file exists.
    assert "server:claude-code-telegrammer" in parsed["spec"]["claude"]["channels"]


def test_create_telegram_block_absent_without_token(tmp_path: Path) -> None:
    # Arrange — no token file -> no bot wiring (unique name avoids the
    # default probe path matching a real secret).
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    runner.invoke(
        create_cmd,
        ["sci-notg-xyz", "--template", "scientist", "--base-dir", str(base)],
    )
    parsed = yaml.safe_load(_spec(base, "sci-notg-xyz").read_text())
    # Assert — no telegrammer channel without a token file.
    assert "server:claude-code-telegrammer" not in parsed["spec"]["claude"]["channels"]


def test_create_render_leaves_no_unresolved_markers(tmp_path: Path) -> None:
    # Arrange — render directly so sentinel handling is exercised in isolation.
    rendered = render_spec(
        name="marker-x",
        template="developer",
        workdir=str(tmp_path),
        group="developer",
        token_path=str(tmp_path / "tok"),
        telegram=False,
    )
    # Act
    leftover = ">>>" in rendered or "<<<" in rendered or "{{" in rendered
    # Assert — every sentinel + token is resolved in the output.
    assert leftover is False


def test_create_refuses_to_overwrite_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe").mkdir(parents=True)
    _spec(base, "dupe").write_text("# pre-existing\n")
    # Act
    result = runner.invoke(create_cmd, ["dupe", "--template", "developer", "--base-dir", str(base)])
    # Assert — non-zero exit so accidental clobber is impossible without --force.
    assert result.exit_code != 0


def test_create_force_overwrites_existing_spec(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    (base / "dupe2").mkdir(parents=True)
    _spec(base, "dupe2").write_text("# stale\n")
    # Act
    runner.invoke(
        create_cmd, ["dupe2", "--template", "developer", "--base-dir", str(base), "--force"]
    )
    text = _spec(base, "dupe2").read_text()
    # Assert — fresh template replaces the stale stub.
    assert "scitex-agent-container/v3" in text


def test_create_rejects_invalid_agent_name(tmp_path: Path) -> None:
    # Arrange — names with slashes would write outside the base dir.
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(
        create_cmd, ["bad/name", "--template", "developer", "--base-dir", str(base)]
    )
    # Assert
    assert result.exit_code != 0


def test_create_rejects_unknown_template(tmp_path: Path) -> None:
    # Arrange
    runner = CliRunner()
    base = tmp_path / "agents"
    # Act
    result = runner.invoke(
        create_cmd, ["whoops", "--template", "nope", "--base-dir", str(base)]
    )
    # Assert — Click's Choice raises UsageError (exit code 2).
    assert result.exit_code != 0
