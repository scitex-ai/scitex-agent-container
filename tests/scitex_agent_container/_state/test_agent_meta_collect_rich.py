"""End-to-end tests of ``agent_meta.collect_rich``.

Covers the transcript JSONL parsing, statusline JSON consumption,
quota fallbacks, account credentials, rotation log writing, machine
metrics, and event-log integration. tmux / subprocess / psutil are
mocked so the test can run without any of them installed.
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
