"""Unit tests for ``agent_meta`` helper functions.

Targets the deterministic helpers in ``_state.agent_meta``:
  - detect_multiplexer (subprocess mocked)
  - _encode_claude_project / _latest_jsonls
  - _parse_skills (CLAUDE.md fenced ```skills block)
  - parse_subagent_count_from_pane_text / _subagent_count_from_pane
  - _capture_pane (truncation + multiplexer guard)
  - _redact_secrets (sk-ant-..., wks_..., token= patterns)
  - _classify_pane_state (every branch)
  - _config_candidates (workspaces sibling, git root, home)
  - _read_claude_md / _read_mcp_json (with redaction)
  - _redact_mcp_tree
  - _parse_mcp_servers
  - _pids_from_session
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scitex_agent_container._state import agent_meta as am


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


# --- detect_multiplexer ---------------------------------------------------


def test_detect_multiplexer_returns_tmux(monkeypatch):
    def fake_run(argv, **kw):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        return m

    monkeypatch.setattr("subprocess.run", fake_run)
    assert am.detect_multiplexer("sess") == "tmux"


def test_detect_multiplexer_returns_screen(monkeypatch):
    def fake_run(argv, **kw):
        m = MagicMock()
        if argv[0] == "tmux":
            m.returncode = 1
            m.stdout = ""
        else:
            m.returncode = 0
            m.stdout = "Sockets in /var/run/.\nMy-sess (Detached)\n"
        return m

    monkeypatch.setattr("subprocess.run", fake_run)
    assert am.detect_multiplexer("My-sess") == "screen"


def test_detect_multiplexer_returns_empty(monkeypatch):
    def fake_run(argv, **kw):
        raise FileNotFoundError("nope")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert am.detect_multiplexer("sess") == ""


# --- _encode_claude_project ----------------------------------------------


def test_encode_claude_project_collapses_triple_dashes():
    enc = am._encode_claude_project("/home/u/.config/foo")
    # / and . both become '-', triple-dashes collapse to '--'
    assert "---" not in enc
    assert enc.startswith("-home-u")


# --- _latest_jsonls ------------------------------------------------------


def test_latest_jsonls_returns_empty_when_dir_missing(tmp_path):
    result = am._latest_jsonls("/nonexistent/path/xyz")
    assert result == []


def test_latest_jsonls_sorts_by_mtime(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    proj_dir = (
        tmp_path
        / ".claude"
        / "projects"
        / am._encode_claude_project(str(workdir.resolve()))
    )
    proj_dir.mkdir(parents=True)
    older = proj_dir / "old.jsonl"
    newer = proj_dir / "new.jsonl"
    older.write_text("a")
    newer.write_text("b")
    import os

    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    files = am._latest_jsonls(str(workdir))
    assert files[0].name == "new.jsonl"


# --- _parse_skills -------------------------------------------------------


def test_parse_skills_extracts_block(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "CLAUDE.md").write_text(
        "intro\n```skills\nskill-a\n# a comment\nskill-b\n```\nrest\n"
    )
    skills = am._parse_skills(str(wd))
    assert skills == ["skill-a", "skill-b"]


def test_parse_skills_returns_empty_when_no_file(tmp_path):
    assert am._parse_skills(str(tmp_path / "nope")) == []


# --- subagent counters ---------------------------------------------------


def test_parse_subagent_count_basic():
    assert am.parse_subagent_count_from_pane_text("foo 3 local agents running\n") == 3
    assert am.parse_subagent_count_from_pane_text("1 local agent still running") == 1
    assert am.parse_subagent_count_from_pane_text("") == 0
    assert am.parse_subagent_count_from_pane_text("nothing relevant") == 0


def test_subagent_count_from_pane_non_tmux():
    assert am._subagent_count_from_pane("sess", "screen") == 0


def test_subagent_count_from_pane_tmux(monkeypatch):
    m = MagicMock()
    m.stdout = "5 local agents running"
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: m)
    assert am._subagent_count_from_pane("sess", "tmux") == 5


def test_subagent_count_from_pane_subprocess_error(monkeypatch):
    def boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr("subprocess.run", boom)
    assert am._subagent_count_from_pane("sess", "tmux") == 0


# --- _capture_pane --------------------------------------------------------


def test_capture_pane_truncates(monkeypatch):
    m = MagicMock()
    m.stdout = "x" * 50000
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: m)
    out = am._capture_pane("sess", "tmux", max_chars=100)
    assert len(out) == 100


def test_capture_pane_non_tmux_returns_empty():
    assert am._capture_pane("sess", "screen") == ""


def test_capture_pane_subprocess_error(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **kw: (_ for _ in ()).throw(OSError("boom"))
    )
    assert am._capture_pane("sess", "tmux") == ""


# --- _redact_secrets -----------------------------------------------------


def test_redact_sk_ant_token():
    s = am._redact_secrets("token sk-ant-abcDEF_-1234 found")
    assert "sk-ant" not in s
    assert "REDACTED" in s


def test_redact_wks_token():
    s = am._redact_secrets("wks_abc123XYZ")
    assert "wks_abc123XYZ" not in s


def test_redact_keyvalue_token():
    s = am._redact_secrets("api_key=secretvalue123")
    assert "secretvalue123" not in s
    assert "REDACTED" in s


def test_redact_secrets_empty():
    assert am._redact_secrets("") == ""


# --- _classify_pane_state ------------------------------------------------


def test_classify_empty():
    assert am._classify_pane_state("") == ("unknown", "")


def test_classify_auth_error():
    state, snippet = am._classify_pane_state("blah\nInvalid API key here\nmore\n")
    assert state == "auth_error"
    assert "Invalid" in snippet


def test_classify_limit_reached():
    state, _ = am._classify_pane_state("blah\nLIMIT REACHED resets in 3h\n")
    assert state == "limit_reached"


def test_classify_yn_prompt():
    state, snip = am._classify_pane_state("Are you sure (y/n)?")
    assert state == "y_n_prompt"
    assert "(y/n)" in snip


def test_classify_compose_pending():
    state, _ = am._classify_pane_state("\n❯ hello there\n")
    assert state == "compose_pending_unsent"


def test_classify_running_prompt():
    state, _ = am._classify_pane_state("text\n❯\n")
    assert state == "running"


def test_classify_unknown_no_prompt():
    state, _ = am._classify_pane_state("just some text without markers\n")
    assert state == "unknown"


# --- _config_candidates --------------------------------------------------


def test_config_candidates_workspaces_sibling(tmp_path):
    workspaces = tmp_path / "workspaces"
    agent_dir = workspaces / "alpha"
    agent_dir.mkdir(parents=True)
    cands = am._config_candidates(str(agent_dir), "CLAUDE.md")
    # mamba-alpha sibling candidate must be present
    assert any("mamba-alpha" in str(c) for c in cands)


def test_config_candidates_dedups(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    cands = am._config_candidates(str(wd), "CLAUDE.md")
    strs = [str(c) for c in cands]
    assert len(strs) == len(set(strs))


def test_config_candidates_git_root(tmp_path):
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    cands = am._config_candidates(str(sub), "CLAUDE.md")
    assert any(str(c) == str(repo / "CLAUDE.md") for c in cands)


# --- _read_claude_md -----------------------------------------------------


def test_read_claude_md_returns_text(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "CLAUDE.md").write_text("hello agents\n")
    assert "hello agents" in am._read_claude_md(str(wd))


def test_read_claude_md_truncates(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "CLAUDE.md").write_text("x" * 50000)
    out = am._read_claude_md(str(wd), max_chars=10)
    assert len(out) == 10


def test_read_claude_md_missing_returns_empty(tmp_path):
    assert am._read_claude_md(str(tmp_path / "nope")) == ""


# --- _redact_mcp_tree ----------------------------------------------------


def test_redact_mcp_tree_redacts_tokens():
    inp = {"API_TOKEN": "abc", "url": "http://x", "nested": {"SECRET_KEY": "z"}}
    out = am._redact_mcp_tree(inp)
    assert out["API_TOKEN"] == "***REDACTED***"
    assert out["nested"]["SECRET_KEY"] == "***REDACTED***"
    assert out["url"] == "http://x"


def test_redact_mcp_tree_walks_lists():
    out = am._redact_mcp_tree([{"PASSWORD": "x"}, "plain"])
    assert out[0]["PASSWORD"] == "***REDACTED***"
    assert out[1] == "plain"


def test_redact_mcp_tree_scalar_passthrough():
    assert am._redact_mcp_tree(5) == 5


# --- _read_mcp_json ------------------------------------------------------


def test_read_mcp_json_returns_redacted(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text(json.dumps({"API_TOKEN": "shh", "x": 1}))
    out = am._read_mcp_json(str(wd))
    assert "shh" not in out
    assert "REDACTED" in out


def test_read_mcp_json_corrupt_falls_back_to_redacted_raw(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text("not-json sk-ant-abcdef")
    out = am._read_mcp_json(str(wd))
    assert "sk-ant-abcdef" not in out


def test_read_mcp_json_missing_returns_empty(tmp_path):
    assert am._read_mcp_json(str(tmp_path / "nope")) == ""


# --- _parse_mcp_servers --------------------------------------------------


def test_parse_mcp_servers_extracts(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "alpha": {"type": "stdio", "command": "bin/serv"},
                    "beta": {"transport": "http", "url": "https://example.com/x"},
                    "bad": "not-a-dict",
                }
            }
        )
    )
    out = am._parse_mcp_servers(str(wd))
    names = {x["name"]: x for x in out}
    assert "alpha" in names and names["alpha"]["command"] == "bin/serv"
    assert names["alpha"]["transport"] == "stdio"
    assert names["beta"]["url_host"] == "example.com"
    assert "bad" not in names


def test_parse_mcp_servers_missing(tmp_path):
    assert am._parse_mcp_servers(str(tmp_path / "nope")) == []


def test_parse_mcp_servers_malformed(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text("{not json")
    assert am._parse_mcp_servers(str(wd)) == []


def test_parse_mcp_servers_non_dict_root(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text("[1,2,3]")
    assert am._parse_mcp_servers(str(wd)) == []


def test_parse_mcp_servers_no_mcpservers_key(tmp_path):
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / ".mcp.json").write_text(json.dumps({"other": {}}))
    assert am._parse_mcp_servers(str(wd)) == []


# --- _pids_from_session --------------------------------------------------


def test_pids_from_session_non_tmux():
    assert am._pids_from_session("sess", "screen") == (0, 0)


def test_pids_from_session_tmux_with_panes(monkeypatch):
    calls = {"n": 0}

    def fake_run(argv, **kw):
        m = MagicMock()
        calls["n"] += 1
        if argv[0] == "tmux":
            m.stdout = "1234\n"
        else:
            # pgrep call
            m.stdout = "5678\n"
        return m

    monkeypatch.setattr("subprocess.run", fake_run)
    pid, ppid = am._pids_from_session("sess", "tmux")
    assert ppid == 1234
    assert pid == 5678


def test_pids_from_session_tmux_error(monkeypatch):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")),
    )
    assert am._pids_from_session("sess", "tmux") == (0, 0)
