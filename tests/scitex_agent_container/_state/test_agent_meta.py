"""Tests for the ``sdk_session`` field in ``agent_meta.collect_rich``.

Covers the read path that surfaces claude-session runtime state on the
status JSON so dashboards and ``sac show-status`` can render quota +
session id without poking at on-disk paths themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state import agent_meta
from scitex_agent_container._runners import claude_session as runner


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    """Redirect the runner's default state root and chdir into a clean
    tmp_path so ``find_project_scope`` walks up from a dir without a
    repo marker — forcing collect_rich to fall through to the
    home-scope state_dir, which we've redirected here."""
    # Two sources of truth post-split (2026-05-03): the runner re-exports
    # DEFAULT_STATE_ROOT from _session_state. ``state_dir_for`` reads the
    # latter at call time, so we have to patch both for the override to
    # take effect.
    from scitex_agent_container._runners import _session_state

    monkeypatch.setattr(runner, "DEFAULT_STATE_ROOT", tmp_path)
    monkeypatch.setattr(_session_state, "DEFAULT_STATE_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_sdk_session_none_when_no_state_dir(
    isolated_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-claude-session agents (no heartbeat.json) → field stays None."""
    payload = agent_meta._read_sdk_session_state("ghost", workdir="/tmp")
    assert payload is None


def test_sdk_session_populated_when_state_present(
    isolated_runtime: Path,
) -> None:
    """heartbeat + quota + session id → all surfaced on the dict."""
    state_dir = isolated_runtime / "alpha"
    runner.write_pid(state_dir, 12345)
    runner.write_heartbeat(state_dir, pid=12345, state=runner.STATE_IDLE)
    runner.write_session_id(state_dir, "sess-abc")
    runner.accumulate_quota(
        state_dir,
        {"input_tokens": 7, "output_tokens": 11, "cache_read_input_tokens": 0},
    )

    payload = agent_meta._read_sdk_session_state("alpha", workdir="/tmp")
    assert payload is not None
    assert payload["session_id"] == "sess-abc"
    assert payload["quota"]["turns"] == 1
    assert payload["quota"]["input_tokens"] == 7
    assert payload["quota"]["output_tokens"] == 11
    assert payload["heartbeat"]["state"] == runner.STATE_IDLE
    assert payload["heartbeat"]["pid"] == 12345
    assert payload["state_dir"].endswith("alpha")


def test_sdk_session_walks_from_cwd_not_workdir(
    isolated_runtime: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``workdir`` may point at /tmp; the read must use cwd to find the
    project scope. We assert this indirectly: with cwd = isolated_runtime
    (no project scope), an agent with state under the runtime default
    is found, even though workdir is something arbitrary."""
    state_dir = isolated_runtime / "beta"
    runner.write_heartbeat(state_dir, pid=999, state=runner.STATE_WORKING)
    payload = agent_meta._read_sdk_session_state("beta", workdir="/some/unrelated/path")
    assert payload is not None
    assert payload["heartbeat"]["state"] == runner.STATE_WORKING


def test_collect_rich_includes_sdk_session_field(
    isolated_runtime: Path,
) -> None:
    """End-to-end: collect_rich() returns a dict that always carries
    the ``sdk_session`` key — None for non-SDK agents, populated for SDK."""
    state_dir = isolated_runtime / "gamma"
    runner.write_heartbeat(state_dir, pid=42, state=runner.STATE_IDLE)
    runner.write_session_id(state_dir, "sid-gamma")

    payload = agent_meta.collect_rich(name="gamma", workdir="/tmp", session="gamma")
    assert "sdk_session" in payload
    assert payload["sdk_session"] is not None
    assert payload["sdk_session"]["session_id"] == "sid-gamma"


# ---------------------------------------------------------------------------
# Merged from test_agent_meta_collect_rich.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scitex_agent_container._state import agent_meta as am


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    """Default: every subprocess.run returns rc=1 stdout='' so the
    helper functions take the 'no multiplexer / no panes' path."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: m)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir()
    return wd


def _seed_transcript(wd: Path, lines: list[dict], home: Path) -> None:
    """Write a Claude Code-style jsonl into the encoded projects dir."""
    proj_dir = (
        home / ".claude" / "projects" / am._encode_claude_project(str(wd.resolve()))
    )
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / "transcript.jsonl"
    jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_collect_rich_minimal_returns_dict(workspace):
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["multiplexer"] == ""
    assert out["pid"] == 0
    assert out["project"] == "alpha"
    assert out["workdir"] == str(workspace)
    # required keys always present
    for k in (
        "context_pct",
        "current_tool",
        "skills_loaded",
        "mcp_servers",
        "metrics",
        "recent_tools",
        "claude_md",
        "mcp_json",
    ):
        assert k in out


def test_collect_rich_parses_transcript(tmp_path, workspace):
    _seed_transcript(
        workspace,
        [
            {"type": "user", "message": {"content": "do something important"}},
            {
                "type": "assistant",
                "timestamp": "2026-05-01T00:00:00Z",
                "message": {
                    "model": "opus-4-7",
                    "usage": {
                        "input_tokens": 100000,
                        "cache_read_input_tokens": 50000,
                        "cache_creation_input_tokens": 25000,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls -la", "description": "list"},
                        },
                    ],
                },
            },
        ],
        home=tmp_path,
    )

    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["model_transcript"] == "opus-4-7"
    assert out["current_tool"] == "Bash"
    assert "list" in out["current_tool_input"] or "ls" in out["current_tool_input"]
    assert out["last_user_msg"].startswith("do something")
    assert out["context_pct"] > 0.0


def test_collect_rich_user_message_as_list_content(tmp_path, workspace):
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
            },
        ],
        home=tmp_path,
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert "list-form prompt" in out["last_user_msg"]


def test_collect_rich_tool_previews_for_each_kind(tmp_path, workspace):
    """Each tool variant in _classify-like switch sets current_tool_input."""
    cases = [
        (
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y.py"}},
            "/x/y.py",
        ),
        (
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "abc.*def"}},
            "abc.*def",
        ),
        (
            {"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*.py"}},
            "**/*.py",
        ),
        (
            {
                "type": "tool_use",
                "name": "Agent",
                "input": {"description": "spin off helper"},
            },
            "spin off helper",
        ),
        (
            {
                "type": "tool_use",
                "name": "mcp__telegram_send",
                "input": {"text": "alert!"},
            },
            "alert!",
        ),
    ]
    for tool_use, expected in cases:
        # Wipe and re-seed for each case
        proj_dir = (
            Path.home()
            / ".claude"
            / "projects"
            / am._encode_claude_project(str(workspace.resolve()))
        )
        if proj_dir.exists():
            for f in proj_dir.glob("*"):
                f.unlink()
        else:
            proj_dir.mkdir(parents=True)
        (proj_dir / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"model": "x", "content": [tool_use]},
                }
            )
            + "\n"
        )
        out = am.collect_rich(name="a", workdir=str(workspace), session="s")
        assert expected in out["current_tool_input"], (
            tool_use,
            out["current_tool_input"],
        )


def test_collect_rich_truncated_jsonl_lines_dont_crash(tmp_path, workspace):
    proj_dir = (
        tmp_path
        / ".claude"
        / "projects"
        / am._encode_claude_project(str(workspace.resolve()))
    )
    proj_dir.mkdir(parents=True)
    (proj_dir / "x.jsonl").write_text("not-json\n{bad json\n")
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["model_transcript"] == ""  # nothing valid parsed
    assert out["current_tool"] == ""


def test_collect_rich_with_statusline_overrides_context_pct(
    tmp_path, workspace, monkeypatch
):
    sl = {
        "model": {"display_name": "Sonnet 4.6"},
        "context_window": {"used_percentage": 42.7},
        "rate_limits": {
            "five_hour": {"used_percentage": 60.1, "resets_at": "2026-05-01T05:00:00Z"},
            "seven_day": {"used_percentage": 12.3, "resets_at": "2026-05-08T00:00:00Z"},
        },
    }
    monkeypatch.setattr(
        "scitex_agent_container.statusline.read_statusline_json",
        lambda name: sl,
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["context_pct"] == 42.7
    assert out["quota_5h_used_pct"] == 60.1
    assert out["quota_7d_used_pct"] == 12.3
    # statusline-provided model bleeds into model_transcript only when JSONL
    # didn't supply one — but model_transcript captures the JSONL value
    # explicitly. With no JSONL, model remains the statusline value but in
    # the agent_meta code the model variable is only emitted as model_transcript.
    assert out["model_transcript"] == "Sonnet 4.6"


def test_collect_rich_quota_error_when_fetch_usage_fails(
    tmp_path, workspace, monkeypatch
):
    # No statusline; fetch_usage raises
    monkeypatch.setattr(
        am,
        "fetch_usage",
        lambda: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert "network down" in (out["quota_error"] or "")


def test_collect_rich_uses_fetch_usage_when_no_statusline(
    tmp_path, workspace, monkeypatch
):
    monkeypatch.setattr(
        am,
        "fetch_usage",
        lambda: {
            "used_pct_5h": 11.0,
            "used_pct_7d": 22.0,
            "reset_at_5h": "x",
            "reset_at_7d": "y",
            "from_cache": True,
            "error": None,
        },
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["quota_5h_used_pct"] == 11.0
    assert out["quota_7d_used_pct"] == 22.0
    assert out["quota_from_cache"] is True


def test_collect_rich_account_credentials_populated(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(
        "scitex_agent_container._account.credentials.read_credentials_metadata",
        lambda: {
            "email_address": "user@x.test",
            "plan_label": "Max 20x",
            "subscription_type": "stripe",
            "rate_limit_tier": "max20",
            "organization_name": "Test Org",
            "account_uuid": "uuid-1",
            "oauth_expires_at": 12345,
            "installed_plugins": ["plugin-a"],
            "status_line_command": "/path/to/statusline",
        },
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["account_email"] == "user@x.test"
    assert out["account_plan_label"] == "Max 20x"
    assert out["installed_plugins"] == ["plugin-a"]
    assert out["status_line_command"] == "/path/to/statusline"
    assert out["oauth_expires_at"] == 12345


def test_collect_rich_rotation_log_appends(tmp_path, workspace, monkeypatch):
    """When oauth_expires_at changes across calls, write an NDJSON line."""
    monkeypatch.setattr(
        "scitex_agent_container._account.credentials.read_credentials_metadata",
        lambda: {
            "email_address": "rot@x.test",
            "plan_label": "Max",
            "account_uuid": "u",
            "oauth_expires_at": 100,
        },
    )

    # Patch local_state.path on the real module so the in-function
    # ``from scitex_config._ecosystem import local_state`` picks it up.
    from scitex_config._ecosystem import local_state as real_local_state

    monkeypatch.setattr(
        real_local_state,
        "path",
        lambda *parts: tmp_path / "scitex-config" / Path(*parts),
    )

    am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Source calls `local_state.path("agent-container", "accounts",
    # "_rotations")`; the patched lambda above joins those onto
    # `tmp_path / "scitex-config" /`, so the full path includes
    # `agent-container/`.
    rot_file = (
        tmp_path
        / "scitex-config"
        / "agent-container"
        / "accounts"
        / "_rotations"
        / "rot@x.test.ndjson"
    )
    assert rot_file.is_file()
    contents = rot_file.read_text().strip().splitlines()
    assert len(contents) == 1
    assert json.loads(contents[0])["oauth_expires_at"] == 100

    # Second call with same expiry: no new line
    am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert len(rot_file.read_text().strip().splitlines()) == 1


def test_collect_rich_metrics_via_psutil(tmp_path, workspace, monkeypatch):
    fake_psutil = MagicMock()
    fake_psutil.cpu_percent.return_value = 7.5
    vm = MagicMock(
        percent=30.0, total=8 * 1024 * 1024 * 1024, available=4 * 1024 * 1024 * 1024
    )
    fake_psutil.virtual_memory.return_value = vm
    disk = MagicMock(percent=55.0, total=10**11, used=5 * 10**10)
    fake_psutil.disk_usage.return_value = disk
    fake_psutil.getloadavg.return_value = (0.5, 1.0, 2.0)
    fake_psutil.cpu_count.return_value = 8
    fake_psutil.cpu_freq.return_value = MagicMock(max=3200.0)
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    metrics = out["metrics"]
    assert metrics["cpu_count"] == 8
    assert metrics["cpu_used_percent"] == 7.5
    assert metrics["mem_used_percent"] == 30.0
    assert metrics["disk_used_percent"] == 55.0
    assert metrics["load_avg_1m"] == 0.5
    assert "3200" in metrics["cpu_model"]


def test_collect_rich_metrics_psutil_missing(tmp_path, workspace, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["metrics"] == {} or isinstance(out["metrics"], dict)


def test_collect_rich_event_log_summary_used(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(
        "scitex_agent_container._state.event_log.summarize",
        lambda name, limit=50: {
            "recent_tools": [{"name": "Bash"}],
            "recent_prompts": [{"text": "hi"}],
            "agent_calls": [],
            "open_agent_calls": [
                {"name": "Agent", "age_seconds": 99},
            ],
            "background_tasks": [],
            "counts": {"Bash": 1},
            "last_tool_at": "2026-01-01T00:00:00Z",
            "last_tool_name": "Bash",
            "last_mcp_tool_at": "",
            "last_mcp_tool_name": "",
        },
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["recent_tools"] == [{"name": "Bash"}]
    assert out["open_agent_calls_count"] == 1
    assert out["oldest_open_agent_age_s"] == 99
    assert out["last_tool_name"] == "Bash"


def test_collect_rich_resolve_hostname_used(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(
        "scitex_agent_container.config._host.resolve_hostname",
        lambda: "nas",
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["machine"] == "nas"


def test_collect_rich_resolve_hostname_failure_falls_back(
    tmp_path, workspace, monkeypatch
):
    def boom():
        raise RuntimeError("yaml missing")

    monkeypatch.setattr(
        "scitex_agent_container.config._host.resolve_hostname",
        boom,
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    # Falls back to gethostname()
    assert isinstance(out["machine"], str)
    assert out["machine"] != ""


def test_collect_rich_started_at_from_jsonl(tmp_path, workspace):
    _seed_transcript(
        workspace,
        [
            {
                "type": "assistant",
                "timestamp": "2026-05-01T00:00:00Z",
                "message": {"model": "m", "content": []},
            },
        ],
        home=tmp_path,
    )
    out = am.collect_rich(name="alpha", workdir=str(workspace), session="sess")
    assert out["started_at_transcript"]  # non-empty ISO string


# ---------------------------------------------------------------------------
# Merged from test_agent_meta_helpers.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

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
