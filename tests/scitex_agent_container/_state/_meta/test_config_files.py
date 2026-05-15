"""Tests for ``_state._meta.config_files`` — CLAUDE.md / .mcp.json readers.

PS-202 src-tests mirror. Real filesystem under ``tmp_path``; HOME
redirected via ``env_save_restore`` so the home-fallback branches of
``_config_candidates`` resolve under the tmp tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._state._meta.config_files import (
    _config_candidates,
    _parse_mcp_servers,
    _read_claude_md,
    _read_mcp_json,
    _redact_mcp_tree,
)

# --- _config_candidates --------------------------------------------------


def test_config_candidates_includes_workdir_path(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    cands = _config_candidates(str(workdir), "CLAUDE.md")
    # Assert
    assert workdir / "CLAUDE.md" in cands


def test_config_candidates_dedupes_paths(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    cands = _config_candidates(str(workdir), "CLAUDE.md")
    # Assert
    assert len(cands) == len(set(str(p) for p in cands))


# --- _read_claude_md ----------------------------------------------------


def test_read_claude_md_returns_contents_when_present(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "CLAUDE.md").write_text("hello\n")
    # Act
    out = _read_claude_md(str(workdir))
    # Assert
    assert out == "hello\n"


def test_read_claude_md_returns_empty_when_absent(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    out = _read_claude_md(str(workdir))
    # Assert
    assert out == ""


def test_read_claude_md_truncates_to_max_chars(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "CLAUDE.md").write_text("x" * 1_000)
    # Act
    out = _read_claude_md(str(workdir), max_chars=50)
    # Assert
    assert len(out) == 50


# --- _redact_mcp_tree ---------------------------------------------------


def test_redact_mcp_tree_redacts_token_field():
    # Arrange
    tree = {"AUTH_TOKEN": "secret123", "url": "https://example.com"}
    # Act
    out = _redact_mcp_tree(tree)
    # Assert
    assert out["AUTH_TOKEN"] == "***REDACTED***"


def test_redact_mcp_tree_preserves_non_secret_keys():
    # Arrange
    tree = {"name": "server", "command": "node"}
    # Act
    out = _redact_mcp_tree(tree)
    # Assert
    assert out == {"name": "server", "command": "node"}


def test_redact_mcp_tree_recurses_into_lists():
    # Arrange
    tree = [{"API_KEY": "x"}, {"name": "y"}]
    # Act
    out = _redact_mcp_tree(tree)
    # Assert
    assert out[0]["API_KEY"] == "***REDACTED***"


# --- _read_mcp_json -----------------------------------------------------


def test_read_mcp_json_returns_empty_when_absent(tmp_path: Path, env_save_restore):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    out = _read_mcp_json(str(workdir))
    # Assert
    assert out == ""


def test_read_mcp_json_redacts_secrets_in_parsed_output(
    tmp_path: Path, env_save_restore
):
    # Arrange
    env_save_restore.set("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s1": {"API_KEY": "leak"}}})
    )
    # Act
    out = _read_mcp_json(str(workdir))
    # Assert
    assert "leak" not in out


# --- _parse_mcp_servers -------------------------------------------------


def test_parse_mcp_servers_returns_empty_for_missing_file(tmp_path: Path):
    # Arrange
    workdir = str(tmp_path)
    # Act
    out = _parse_mcp_servers(workdir)
    # Assert
    assert out == []


def test_parse_mcp_servers_extracts_server_entries(tmp_path: Path):
    # Arrange
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {
                        "type": "http",
                        "url": "https://api.example.com/foo",
                        "command": "node",
                    }
                }
            }
        )
    )
    # Act
    out = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert out == [
        {
            "name": "alpha",
            "transport": "http",
            "url_host": "api.example.com",
            "command": "node",
        }
    ]


def test_parse_mcp_servers_returns_empty_on_malformed_json(tmp_path: Path):
    # Arrange
    (tmp_path / ".mcp.json").write_text("{not json")
    # Act
    out = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert out == []
