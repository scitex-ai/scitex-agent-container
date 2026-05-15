"""No-mocks tests for ``scitex_agent_container._state.agent_meta`` and its
``_meta/`` helper submodules.

Seam strategy (PA-306 + TQ001/002/003/007 compliance):

* Filesystem isolation via ``HOME`` env override (``env_save_restore`` fixture)
  so ``Path.home()`` resolves into ``tmp_path``. Real CLAUDE.md, .mcp.json,
  ``~/.claude/projects/<encoded>/*.jsonl`` files are written and read back.
* ``subprocess_shim`` installs real fake binaries on ``$PATH`` for tmux /
  screen / pgrep — production code invokes the real ``subprocess.run`` and
  finds the shim via PATH lookup.
* SDK-session tests use a real ``scitex_config._ecosystem.local_state``
  project scope: create ``<tmp>/.git/`` + ``<tmp>/.scitex/agent-container/``
  and chdir there so ``find_project_scope`` returns the tmp scope.
* Statusline is wired through the real ``SAC_STATUSLINE_STATE_DIR`` env seam
  (the production module re-reads it on every call).
* Hostname is wired through the ``SCITEX_AGENT_CONTAINER_HOSTNAME`` env seam.

No ``unittest.mock``, no ``MagicMock``, no ``monkeypatch``, no ``mocker``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._state import agent_meta as am
from scitex_agent_container._state._meta import (
    config_files as cf_mod,
)
from scitex_agent_container._state._meta import (
    pane as pane_mod,
)
from scitex_agent_container._state._meta import (
    quota as quota_mod,
)
from scitex_agent_container._state._meta import (
    resources as res_mod,
)
from scitex_agent_container._state._meta import (
    secrets as sec_mod,
)
from scitex_agent_container._state._meta import (
    skills as sk_mod,
)
from scitex_agent_container._state._meta import (
    transcript as tr_mod,
)

# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chdir_restore():
    """Chdir helper that auto-restores on teardown."""
    saved = os.getcwd()

    def _chdir(target: Path) -> None:
        os.chdir(target)

    try:
        yield _chdir
    finally:
        os.chdir(saved)


@pytest.fixture
def isolated_home(tmp_path: Path, env_save_restore) -> Path:
    """Redirect $HOME into ``tmp_path`` so ``Path.home()`` is sandboxed."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Real on-disk workspace directory."""
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


@pytest.fixture
def isolated_local_state(tmp_path: Path, env_save_restore, chdir_restore):
    """A real scitex-config local-state scope at ``tmp_path``.

    Sets up ``<tmp>/.git/`` + ``<tmp>/.scitex/agent-container/`` and points
    ``SCITEX_DIR`` at ``<tmp>/.scitex_user`` so the fallback user-scope
    is also under tmp. cwd is changed into tmp so ``find_project_scope``
    walks the tmp git root.
    """
    (tmp_path / ".git").mkdir()
    (tmp_path / ".scitex" / "agent-container").mkdir(parents=True)
    env_save_restore.set("SCITEX_DIR", str(tmp_path / ".scitex_user"))
    chdir_restore(tmp_path)
    return tmp_path / ".scitex" / "agent-container"


def _seed_transcript(wd: Path, lines: list[dict], home: Path) -> None:
    """Write a Claude Code-style jsonl into the encoded projects dir."""
    proj_dir = (
        home / ".claude" / "projects" / am._encode_claude_project(str(wd.resolve()))
    )
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / "transcript.jsonl"
    jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


# ---------------------------------------------------------------------------
# _encode_claude_project (pure)
# ---------------------------------------------------------------------------


def test_encode_claude_project_collapses_triple_dashes():
    # Arrange
    workdir = "/home/u/.config/foo"
    # Act
    enc = tr_mod._encode_claude_project(workdir)
    # Assert
    assert "---" not in enc and enc.startswith("-home-u")


# ---------------------------------------------------------------------------
# _latest_jsonls (real filesystem)
# ---------------------------------------------------------------------------


def test_latest_jsonls_returns_empty_when_dir_missing():
    # Arrange
    nonexistent = "/nonexistent/path/xyz"
    # Act
    result = tr_mod._latest_jsonls(nonexistent)
    # Assert
    assert result == []


def test_latest_jsonls_sorts_newest_first(isolated_home: Path, workspace: Path):
    # Arrange
    proj_dir = (
        isolated_home
        / ".claude"
        / "projects"
        / am._encode_claude_project(str(workspace.resolve()))
    )
    proj_dir.mkdir(parents=True)
    older = proj_dir / "old.jsonl"
    newer = proj_dir / "new.jsonl"
    older.write_text("a")
    newer.write_text("b")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    # Act
    files = tr_mod._latest_jsonls(str(workspace))
    # Assert
    assert files[0].name == "new.jsonl"


# ---------------------------------------------------------------------------
# _parse_skills (real filesystem)
# ---------------------------------------------------------------------------


def test_parse_skills_extracts_skill_lines(tmp_path: Path):
    # Arrange
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "CLAUDE.md").write_text(
        "intro\n```skills\nskill-a\n# a comment\nskill-b\n```\nrest\n"
    )
    # Act
    skills = sk_mod._parse_skills(str(wd))
    # Assert
    assert skills == ["skill-a", "skill-b"]


def test_parse_skills_returns_empty_when_no_file(tmp_path: Path):
    # Arrange
    target = tmp_path / "nope"
    # Act
    skills = sk_mod._parse_skills(str(target))
    # Assert
    assert skills == []


# ---------------------------------------------------------------------------
# Subagent counter (pure regex + subprocess_shim)
# ---------------------------------------------------------------------------


def test_parse_subagent_count_extracts_number_from_marker():
    # Arrange
    pane = "foo 3 local agents running\n"
    # Act
    count = pane_mod.parse_subagent_count_from_pane_text(pane)
    # Assert
    assert count == 3


def test_parse_subagent_count_handles_singular_form():
    # Arrange
    pane = "1 local agent still running"
    # Act
    count = pane_mod.parse_subagent_count_from_pane_text(pane)
    # Assert
    assert count == 1


def test_parse_subagent_count_zero_when_no_marker():
    # Arrange
    pane = "nothing relevant here"
    # Act
    count = pane_mod.parse_subagent_count_from_pane_text(pane)
    # Assert
    assert count == 0


def test_subagent_count_from_pane_returns_zero_for_non_tmux():
    # Arrange
    multiplexer = "screen"
    # Act
    count = pane_mod._subagent_count_from_pane("sess", multiplexer)
    # Assert
    assert count == 0


def test_subagent_count_from_pane_invokes_tmux_capture(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="5 local agents running\n")
    # Act
    count = pane_mod._subagent_count_from_pane("sess", "tmux")
    # Assert
    assert count == 5


def test_subagent_count_from_pane_returns_zero_on_subprocess_error(subprocess_shim):
    # Arrange — exit 1, no stdout → parse 0 (falls through).
    subprocess_shim.install("tmux", exit=1, stdout="")
    # Act
    count = pane_mod._subagent_count_from_pane("sess", "tmux")
    # Assert
    assert count == 0


# ---------------------------------------------------------------------------
# _capture_pane (subprocess_shim)
# ---------------------------------------------------------------------------


def test_capture_pane_returns_empty_for_non_tmux():
    # Arrange
    multiplexer = "screen"
    # Act
    out = pane_mod._capture_pane("sess", multiplexer)
    # Assert
    assert out == ""


def test_capture_pane_truncates_to_max_chars(subprocess_shim):
    # Arrange
    subprocess_shim.install("tmux", stdout="x" * 50_000)
    # Act
    out = pane_mod._capture_pane("sess", "tmux", max_chars=100)
    # Assert
    assert len(out) == 100


# ---------------------------------------------------------------------------
# _redact_secrets (pure)
# ---------------------------------------------------------------------------


def test_redact_secrets_strips_sk_ant_token():
    # Arrange
    text = "token sk-ant-abcDEF_-1234 found"
    # Act
    redacted = sec_mod._redact_secrets(text)
    # Assert
    assert "sk-ant-abc" not in redacted


def test_redact_secrets_strips_wks_token():
    # Arrange
    text = "wks_abc123XYZ"
    # Act
    redacted = sec_mod._redact_secrets(text)
    # Assert
    assert "wks_abc123XYZ" not in redacted


def test_redact_secrets_redacts_keyvalue_pair():
    # Arrange
    text = "api_key=secretvalue123"
    # Act
    redacted = sec_mod._redact_secrets(text)
    # Assert
    assert "secretvalue123" not in redacted


def test_redact_secrets_empty_input_returns_empty():
    # Arrange
    text = ""
    # Act
    redacted = sec_mod._redact_secrets(text)
    # Assert
    assert redacted == ""


# ---------------------------------------------------------------------------
# _classify_pane_state (pure)
# ---------------------------------------------------------------------------


def test_classify_pane_state_empty_is_unknown():
    # Arrange
    pane = ""
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "unknown"


def test_classify_pane_state_detects_auth_error():
    # Arrange
    pane = "blah\nInvalid API key here\nmore\n"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "auth_error"


def test_classify_pane_state_detects_limit_reached():
    # Arrange
    pane = "blah\nLIMIT REACHED resets in 3h\n"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "limit_reached"


def test_classify_pane_state_detects_yn_prompt():
    # Arrange
    pane = "Are you sure (y/n)?"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "y_n_prompt"


def test_classify_pane_state_detects_compose_pending_unsent():
    # Arrange
    pane = "\nbase\n\u276f hello there\n"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "compose_pending_unsent"


def test_classify_pane_state_detects_running_when_prompt_only():
    # Arrange
    pane = "text\n\u276f\n"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "running"


def test_classify_pane_state_unknown_without_markers():
    # Arrange
    pane = "just some text without markers\n"
    # Act
    state, _ = pane_mod._classify_pane_state(pane)
    # Assert
    assert state == "unknown"


# ---------------------------------------------------------------------------
# _config_candidates (real filesystem)
# ---------------------------------------------------------------------------


def test_config_candidates_includes_workspaces_mamba_sibling(
    isolated_home: Path, tmp_path: Path
):
    # Arrange
    workspaces = tmp_path / "workspaces"
    agent_dir = workspaces / "alpha"
    agent_dir.mkdir(parents=True)
    # Act
    cands = cf_mod._config_candidates(str(agent_dir), "CLAUDE.md")
    # Assert
    assert any("mamba-alpha" in str(c) for c in cands)


def test_config_candidates_dedups_paths(isolated_home: Path, tmp_path: Path):
    # Arrange
    wd = tmp_path / "ws"
    wd.mkdir()
    # Act
    cands = cf_mod._config_candidates(str(wd), "CLAUDE.md")
    # Assert
    strs = [str(c) for c in cands]
    assert len(strs) == len(set(strs))


def test_config_candidates_walks_to_git_root(isolated_home: Path, tmp_path: Path):
    # Arrange
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    # Act
    cands = cf_mod._config_candidates(str(sub), "CLAUDE.md")
    # Assert
    assert any(str(c) == str(repo / "CLAUDE.md") for c in cands)


# ---------------------------------------------------------------------------
# _read_claude_md (real filesystem)
# ---------------------------------------------------------------------------


def test_read_claude_md_returns_workspace_file_content(
    isolated_home: Path, workspace: Path
):
    # Arrange
    (workspace / "CLAUDE.md").write_text("hello agents\n")
    # Act
    content = cf_mod._read_claude_md(str(workspace))
    # Assert
    assert "hello agents" in content


def test_read_claude_md_truncates_to_max_chars(isolated_home: Path, workspace: Path):
    # Arrange
    (workspace / "CLAUDE.md").write_text("x" * 50_000)
    # Act
    content = cf_mod._read_claude_md(str(workspace), max_chars=10)
    # Assert
    assert len(content) == 10


def test_read_claude_md_returns_empty_when_missing(isolated_home: Path, tmp_path: Path):
    # Arrange
    target = tmp_path / "nope"
    # Act
    content = cf_mod._read_claude_md(str(target))
    # Assert
    assert content == ""


# ---------------------------------------------------------------------------
# _redact_mcp_tree (pure)
# ---------------------------------------------------------------------------


def test_redact_mcp_tree_redacts_token_keys_at_top_level():
    # Arrange
    inp = {"API_TOKEN": "abc", "url": "http://x"}
    # Act
    out = cf_mod._redact_mcp_tree(inp)
    # Assert
    assert out["API_TOKEN"] == "***REDACTED***"


def test_redact_mcp_tree_walks_lists():
    # Arrange
    inp = [{"PASSWORD": "x"}, "plain"]
    # Act
    out = cf_mod._redact_mcp_tree(inp)
    # Assert
    assert out[0]["PASSWORD"] == "***REDACTED***"


def test_redact_mcp_tree_scalar_passthrough():
    # Arrange
    inp = 5
    # Act
    out = cf_mod._redact_mcp_tree(inp)
    # Assert
    assert out == 5


# ---------------------------------------------------------------------------
# _read_mcp_json (real filesystem)
# ---------------------------------------------------------------------------


def test_read_mcp_json_returns_redacted_pretty(isolated_home: Path, workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text(json.dumps({"API_TOKEN": "shh", "x": 1}))
    # Act
    out = cf_mod._read_mcp_json(str(workspace))
    # Assert
    assert "shh" not in out and "REDACTED" in out


def test_read_mcp_json_corrupt_falls_back_to_redacted_raw(
    isolated_home: Path, workspace: Path
):
    # Arrange
    (workspace / ".mcp.json").write_text("not-json sk-ant-abcdef")
    # Act
    out = cf_mod._read_mcp_json(str(workspace))
    # Assert
    assert "sk-ant-abcdef" not in out


def test_read_mcp_json_missing_returns_empty(isolated_home: Path, tmp_path: Path):
    # Arrange
    target = tmp_path / "nope"
    # Act
    out = cf_mod._read_mcp_json(str(target))
    # Assert
    assert out == ""


# ---------------------------------------------------------------------------
# _parse_mcp_servers (real filesystem)
# ---------------------------------------------------------------------------


def test_parse_mcp_servers_extracts_stdio_command(workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"alpha": {"type": "stdio", "command": "bin/serv"}}})
    )
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers[0]["command"] == "bin/serv"


def test_parse_mcp_servers_extracts_http_url_host(workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "beta": {"transport": "http", "url": "https://example.com/x"}
                }
            }
        )
    )
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers[0]["url_host"] == "example.com"


def test_parse_mcp_servers_skips_non_dict_entries(workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"bad": "not-a-dict"}})
    )
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers == []


def test_parse_mcp_servers_returns_empty_for_missing_file(tmp_path: Path):
    # Arrange
    target = tmp_path / "nope"
    # Act
    servers = cf_mod._parse_mcp_servers(str(target))
    # Assert
    assert servers == []


def test_parse_mcp_servers_returns_empty_for_malformed_json(workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text("{not json")
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers == []


def test_parse_mcp_servers_returns_empty_when_root_is_not_dict(workspace: Path):
    # Arrange
    (workspace / ".mcp.json").write_text("[1,2,3]")
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers == []


def test_parse_mcp_servers_returns_empty_without_mcpservers_key(
    workspace: Path,
):
    # Arrange
    (workspace / ".mcp.json").write_text(json.dumps({"other": {}}))
    # Act
    servers = cf_mod._parse_mcp_servers(str(workspace))
    # Assert
    assert servers == []


# ---------------------------------------------------------------------------
# _pids_from_session (subprocess_shim)
# ---------------------------------------------------------------------------


def test_pids_from_session_non_tmux_returns_zero_zero():
    # Arrange
    multiplexer = "screen"
    # Act
    pids = res_mod._pids_from_session("sess", multiplexer)
    # Assert
    assert pids == (0, 0)


def test_pids_from_session_tmux_resolves_pane_pid(subprocess_shim):
    # Arrange — tmux returns pane pid; pgrep returns the child claude pid.
    subprocess_shim.install("tmux", stdout="1234\n")
    subprocess_shim.install("pgrep", stdout="5678\n")
    # Act
    pid, ppid = res_mod._pids_from_session("sess", "tmux")
    # Assert
    assert (pid, ppid) == (5_678, 1_234)


def test_pids_from_session_swallows_subprocess_error(subprocess_shim):
    # Arrange — empty stdout makes int("") raise, caught by the helper.
    subprocess_shim.install("tmux", stdout="")
    # Act
    pids = res_mod._pids_from_session("sess", "tmux")
    # Assert
    assert pids == (0, 0)


# ---------------------------------------------------------------------------
# detect_multiplexer (subprocess_shim)
# ---------------------------------------------------------------------------


def test_detect_multiplexer_returns_tmux_when_tmux_has_session_succeeds(
    subprocess_shim,
):
    # Arrange
    subprocess_shim.install("tmux", exit=0)
    # Act
    result = am.detect_multiplexer("sess")
    # Assert
    assert result == "tmux"


def test_detect_multiplexer_returns_screen_when_tmux_fails_screen_lists(
    subprocess_shim,
):
    # Arrange
    subprocess_shim.install("tmux", exit=1)
    subprocess_shim.install(
        "screen", stdout="Sockets in /var/run/.\nMy-sess (Detached)\n"
    )
    # Act
    result = am.detect_multiplexer("My-sess")
    # Assert
    assert result == "screen"


def test_detect_multiplexer_returns_empty_when_neither_present(
    tmp_path: Path, env_save_restore
):
    # Arrange — PATH points at an empty dir (no tmux/screen).
    empty = tmp_path / "empty_bin"
    empty.mkdir()
    env_save_restore.set("PATH", str(empty))
    # Act
    result = am.detect_multiplexer("sess")
    # Assert
    assert result == ""


# ---------------------------------------------------------------------------
# _collect_host_metrics (real psutil — optional dependency)
# ---------------------------------------------------------------------------


def test_collect_host_metrics_returns_expected_keys():
    # Arrange
    pytest.importorskip("psutil")
    expected = {
        "cpu_count",
        "cpu_used_percent",
        "mem_used_percent",
        "disk_used_percent",
    }
    # Act
    metrics = res_mod._collect_host_metrics()
    # Assert
    assert expected.issubset(metrics.keys())


def test_collect_host_metrics_cpu_count_is_positive_integer():
    # Arrange
    pytest.importorskip("psutil")
    # Act
    metrics = res_mod._collect_host_metrics()
    # Assert
    assert isinstance(metrics["cpu_count"], int) and metrics["cpu_count"] > 0


# ---------------------------------------------------------------------------
# _quota_from_statusline (pure)
# ---------------------------------------------------------------------------


def test_quota_from_statusline_returns_empty_when_no_input():
    # Arrange
    sl: dict = {}
    # Act
    out = quota_mod._quota_from_statusline(sl)
    # Assert
    assert out == {}


def test_quota_from_statusline_rounds_five_hour_pct():
    # Arrange
    sl = {"rate_limits": {"five_hour": {"used_percentage": 60.111}}}
    # Act
    out = quota_mod._quota_from_statusline(sl)
    # Assert
    assert out["quota_5h_used_pct"] == 60.1


def test_quota_from_statusline_rounds_seven_day_pct():
    # Arrange
    sl = {"rate_limits": {"seven_day": {"used_percentage": 12.345}}}
    # Act
    out = quota_mod._quota_from_statusline(sl)
    # Assert
    assert out["quota_7d_used_pct"] == 12.3


# ---------------------------------------------------------------------------
# collect_quota_and_account — real HOME-isolated credentials read
# ---------------------------------------------------------------------------


def test_collect_quota_and_account_missing_credentials_yields_none_email(
    isolated_home: Path,
):
    # Arrange — no ~/.claude/.credentials.json under tmp HOME.
    sl: dict = {}
    # Act
    out = quota_mod.collect_quota_and_account(sl)
    # Assert
    assert out["account_email"] is None


def test_collect_quota_and_account_reads_email_from_real_claude_json(
    isolated_home: Path,
):
    # Arrange — write a real ~/.claude.json with oauthAccount block.
    (isolated_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "user@x.test"}})
    )
    # Act
    out = quota_mod.collect_quota_and_account({})
    # Assert
    assert out["account_email"] == "user@x.test"


# ---------------------------------------------------------------------------
# SDK session — real claude-session state under local-state scope
# ---------------------------------------------------------------------------


def test_read_sdk_session_state_none_when_no_heartbeat(
    isolated_local_state: Path,
):
    # Arrange — no runtime/<name>/heartbeat.json present.
    # Act
    payload = tr_mod._read_sdk_session_state("ghost", workdir="/tmp")
    # Assert
    assert payload is None


def test_read_sdk_session_state_returns_dict_when_heartbeat_present(
    isolated_local_state: Path,
):
    # Arrange — drop a real heartbeat into the scope runtime dir.
    from scitex_agent_container._runners import _session_state as ss

    state_dir = isolated_local_state / "runtime" / "alpha"
    ss.write_heartbeat(state_dir, pid=12_345, state=ss.STATE_IDLE)
    # Act
    payload = tr_mod._read_sdk_session_state("alpha", workdir="/tmp")
    # Assert
    assert payload is not None and payload["heartbeat"]["pid"] == 12_345


def test_read_sdk_session_state_surfaces_session_id(
    isolated_local_state: Path,
):
    # Arrange
    from scitex_agent_container._runners import _session_state as ss

    state_dir = isolated_local_state / "runtime" / "alpha"
    ss.write_heartbeat(state_dir, pid=12_345, state=ss.STATE_IDLE)
    ss.write_session_id(state_dir, "sess-abc")
    # Act
    payload = tr_mod._read_sdk_session_state("alpha", workdir="/tmp")
    # Assert
    assert payload["session_id"] == "sess-abc"


def test_read_sdk_session_state_surfaces_accumulated_quota_turns(
    isolated_local_state: Path,
):
    # Arrange
    from scitex_agent_container._runners import _session_state as ss

    state_dir = isolated_local_state / "runtime" / "alpha"
    ss.write_heartbeat(state_dir, pid=1, state=ss.STATE_IDLE)
    ss.accumulate_quota(state_dir, {"input_tokens": 7, "output_tokens": 11})
    # Act
    payload = tr_mod._read_sdk_session_state("alpha", workdir="/tmp")
    # Assert
    assert payload["quota"]["turns"] == 1


# ---------------------------------------------------------------------------
# collect_rich — end-to-end smoke + key fields
# ---------------------------------------------------------------------------


@pytest.fixture
def collect_rich_env(isolated_home, env_save_restore, tmp_path):
    """Common env setup for collect_rich tests — PATH cleared so no tmux.

    Returns the home directory; tests use ``workspace`` for the workdir."""
    empty_bin = tmp_path / "_no_bin"
    empty_bin.mkdir(exist_ok=True)
    env_save_restore.set("PATH", str(empty_bin))
    env_save_restore.set("SCITEX_DIR", str(isolated_home / "_scitex"))
    return isolated_home


def test_collect_rich_returns_workdir_field(collect_rich_env: Path, workspace: Path):
    # Arrange
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["workdir"] == str(workspace)


def test_collect_rich_returns_project_name(collect_rich_env: Path, workspace: Path):
    # Arrange
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["project"] == "alpha"


def test_collect_rich_no_multiplexer_when_path_empty(
    collect_rich_env: Path, workspace: Path
):
    # Arrange
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["multiplexer"] == ""


def test_collect_rich_parses_transcript_model(collect_rich_env: Path, workspace: Path):
    # Arrange — seed a real JSONL with an assistant turn carrying a model id.
    _seed_transcript(
        workspace,
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-01T00:00:00Z",
                "message": {"model": "opus-4-7", "content": []},
            }
        ],
        home=collect_rich_env,
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["model_transcript"] == "opus-4-7"


def test_collect_rich_parses_last_user_message(collect_rich_env: Path, workspace: Path):
    # Arrange
    _seed_transcript(
        workspace,
        [{"type": "user", "message": {"content": "do something important"}}],
        home=collect_rich_env,
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["last_user_msg"].startswith("do something")


def test_collect_rich_user_message_list_content_extracts_text_only(
    collect_rich_env: Path, workspace: Path
):
    # Arrange — content as list with text + tool_result; tool_result is skipped.
    _seed_transcript(
        workspace,
        [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "list-form prompt"},
                        {"type": "tool_result", "data": "ignored"},
                    ]
                },
            }
        ],
        home=collect_rich_env,
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert "list-form prompt" in out["last_user_msg"]


def test_collect_rich_extracts_current_tool_bash(
    collect_rich_env: Path, workspace: Path
):
    # Arrange
    _seed_transcript(
        workspace,
        [
            {
                "type": "assistant",
                "message": {
                    "model": "m",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"description": "list dir", "command": "ls -la"},
                        }
                    ],
                },
            }
        ],
        home=collect_rich_env,
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["current_tool"] == "Bash"


def test_collect_rich_truncated_jsonl_does_not_crash(
    collect_rich_env: Path, workspace: Path
):
    # Arrange — invalid JSON lines must be tolerated, not crash.
    proj_dir = (
        collect_rich_env
        / ".claude"
        / "projects"
        / am._encode_claude_project(str(workspace.resolve()))
    )
    proj_dir.mkdir(parents=True)
    (proj_dir / "x.jsonl").write_text("not-json\n{bad json\n")
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["model_transcript"] == ""


def test_collect_rich_statusline_overrides_context_pct(
    collect_rich_env: Path, workspace: Path, env_save_restore
):
    # Arrange — write a real statusline JSON via SAC_STATUSLINE_STATE_DIR.
    state_dir = collect_rich_env / "statusline"
    state_dir.mkdir()
    env_save_restore.set("SAC_STATUSLINE_STATE_DIR", str(state_dir))
    (state_dir / "alpha.json").write_text(
        json.dumps(
            {
                "model": {"display_name": "Sonnet 4.6"},
                "context_window": {"used_percentage": 42.7},
            }
        )
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["context_pct"] == 42.7


def test_collect_rich_statusline_supplies_quota_5h_used_pct(
    collect_rich_env: Path, workspace: Path, env_save_restore
):
    # Arrange
    state_dir = collect_rich_env / "statusline"
    state_dir.mkdir()
    env_save_restore.set("SAC_STATUSLINE_STATE_DIR", str(state_dir))
    (state_dir / "alpha.json").write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 60.1}}})
    )
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["quota_5h_used_pct"] == 60.1


def test_collect_rich_resolves_hostname_via_env_seam(
    collect_rich_env: Path, workspace: Path, env_save_restore
):
    # Arrange — production reads SCITEX_AGENT_CONTAINER_HOSTNAME first.
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "nas-test")
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert out["machine"] == "nas-test"


def test_collect_rich_metrics_dict_has_cpu_count(
    collect_rich_env: Path, workspace: Path
):
    # Arrange — psutil is real on the test host.
    pytest.importorskip("psutil")
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert "cpu_count" in out["metrics"]


def test_collect_rich_returns_claude_md_content_when_present(
    collect_rich_env: Path, workspace: Path
):
    # Arrange
    (workspace / "CLAUDE.md").write_text("hello agents\n")
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert "hello agents" in out["claude_md"]


def test_collect_rich_includes_sdk_session_key(collect_rich_env: Path, workspace: Path):
    # Arrange — even without a heartbeat, the key must be present.
    # Act
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Assert
    assert "sdk_session" in out
