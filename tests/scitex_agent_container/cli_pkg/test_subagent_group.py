"""CLI tests for the ``sac subagent`` noun-group.

Mirrors the MCP tool surface: ``sac subagent get-state`` and
``subagent_get_state`` share the same underlying function, so we only
exercise the CLI-shaped concerns here (help text, flag wiring, JSON
vs table output, project_path threading). The pure-state filesystem
contract is covered by ``tests/.../test__subagent.py``.

The CLI exposes a hidden ``--projects-root`` flag (an escape hatch
mirroring the function's kwarg) so tests can point sac at a tmp_path
projects tree without monkey-patching env or stdlib internals.

TQ rules:
  * AAA marker comments on every test (TQ002).
  * One assertion per test (TQ007). Helpers share fixture setup.
  * Behaviour-shaped names (TQ003).
  * No ``mocker`` / ``monkeypatch`` (STX-NM002).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.subagent_group import subagent_group

# ─── fixtures ────────────────────────────────────────────────────────────────


PROJECT_PATH = "/tmp/fake-cli-proj-for-tests"
PROJECT_HASH = "-tmp-fake-cli-proj-for-tests"
SESSION = "33333333-3333-4333-8333-333333333333"


def _make_projects_root_with_agent(tmp_path: Path, agent_id: str = "cli-aaa") -> Path:
    """Build a ``~/.claude/projects/<hash>/<sess>/subagents/`` tree under
    ``tmp_path`` with one Claude Code subagent transcript and return
    the projects-root path (the part tests pass to --projects-root)."""
    root = tmp_path / "claude-projects"
    base = root / PROJECT_HASH / SESSION / "subagents"
    base.mkdir(parents=True)
    rec_user = {
        "type": "user",
        "message": {"role": "user", "content": "CLI test description"},
        "timestamp": "2026-05-15T12:00:00Z",
    }
    rec_asst = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "input": {}}],
        },
        "timestamp": "2026-05-15T12:00:01Z",
    }
    path = base / f"agent-{agent_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec_user) + "\n")
        fh.write(json.dumps(rec_asst) + "\n")
    return root


def _add_extra_agent(root: Path, agent_id: str, content: str = "other") -> None:
    """Append a second subagent transcript under the same session."""
    base = root / PROJECT_HASH / SESSION / "subagents"
    path = base / f"agent-{agent_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "timestamp": "2026-05-15T12:00:00Z",
                }
            )
            + "\n"
        )


# ─── --help wiring ────────────────────────────────────────────────────────────


def test_subagent_group_help_exit_zero() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["--help"])
    # Assert
    assert result.exit_code == 0


def test_subagent_group_help_lists_get_state() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["--help"])
    # Assert
    assert "get-state" in result.output


def test_get_state_help_exit_zero() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert result.exit_code == 0


def test_get_state_help_advertises_agent_id_flag() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert "--agent-id" in result.output


def test_get_state_help_advertises_project_path_flag() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert "--project-path" in result.output


def test_get_state_help_advertises_session_id_flag() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert "--session-id" in result.output


def test_get_state_help_advertises_json_flag() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert "--json" in result.output


def test_get_state_help_advertises_projects_root_flag() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(subagent_group, ["get-state", "--help"])
    # Assert
    assert "--projects-root" in result.output


# ─── JSON output mirrors the MCP tool ────────────────────────────────────────


def test_get_state_json_empty_when_projects_root_missing(tmp_path) -> None:
    # Arrange — point at a directory that has no project hash dir inside.
    empty_root = tmp_path / "no-projects-here"
    empty_root.mkdir()
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(empty_root),
            "--json",
        ],
    )
    # Assert
    assert json.loads(result.output) == []


def test_get_state_json_returns_one_row_per_subagent(tmp_path) -> None:
    # Arrange
    root = _make_projects_root_with_agent(tmp_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(root),
            "--json",
        ],
    )
    # Assert
    payload = json.loads(result.output)
    assert len(payload) == 1


def test_get_state_json_carries_description(tmp_path) -> None:
    # Arrange
    root = _make_projects_root_with_agent(tmp_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(root),
            "--json",
        ],
    )
    # Assert
    payload = json.loads(result.output)
    assert payload[0]["description"] == "CLI test description"


def test_get_state_json_carries_last_tool(tmp_path) -> None:
    # Arrange
    root = _make_projects_root_with_agent(tmp_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(root),
            "--json",
        ],
    )
    # Assert
    payload = json.loads(result.output)
    assert payload[0]["last_tool"] == "Read"


def test_get_state_agent_id_filter_restricts_payload(tmp_path) -> None:
    # Arrange — two subagents, ask for only one by id.
    root = _make_projects_root_with_agent(tmp_path, agent_id="cli-aaa")
    _add_extra_agent(root, "cli-bbb")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(root),
            "--agent-id",
            "cli-aaa",
            "--json",
        ],
    )
    # Assert
    payload = json.loads(result.output)
    assert [r["id"] for r in payload] == ["cli-aaa"]


# ─── Table output (default, no --json) ───────────────────────────────────────


def test_get_state_table_includes_subagent_id(tmp_path) -> None:
    # Arrange
    root = _make_projects_root_with_agent(tmp_path, agent_id="cli-id-shows")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(root),
        ],
    )
    # Assert
    assert "cli-id-shows" in result.output


def test_get_state_table_empty_message_when_no_subagents(tmp_path) -> None:
    # Arrange — projects-root with no project hash dir inside.
    empty_root = tmp_path / "no-projects-here"
    empty_root.mkdir()
    runner = CliRunner()
    # Act
    result = runner.invoke(
        subagent_group,
        [
            "get-state",
            "--project-path",
            PROJECT_PATH,
            "--projects-root",
            str(empty_root),
        ],
    )
    # Assert
    assert "no Claude Code subagents found" in result.output
