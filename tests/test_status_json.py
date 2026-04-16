"""Tests for rich metadata collection in ``status --json``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scitex_agent_container import agent_meta


# ---------------------------------------------------------------------------
# collect_rich — unit tests with fake workspace, no live tmux required
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "fake-agent"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text(
        "# header\n\n"
        "```skills\n"
        "scitex\n"
        "scitex-orochi\n"
        "# a comment\n"
        "scitex-agent-container\n"
        "```\n"
    )
    return ws


def test_collect_rich_shape(fake_workspace: Path) -> None:
    with patch.object(agent_meta, "detect_multiplexer", return_value=""):
        rich = agent_meta.collect_rich(
            name="fake-agent",
            workdir=str(fake_workspace),
            session="fake-agent",
        )
    # All required keys present
    for key in [
        "multiplexer",
        "pid",
        "ppid",
        "subagent_count",
        "subagents",
        "context_pct",
        "current_tool",
        "current_task",
        "last_activity",
        "skills_loaded",
        "machine",
        "workdir",
        "project",
        "started_at_transcript",
        "model_transcript",
        "version",
    ]:
        assert key in rich, f"missing {key}"

    # Defaults when no tmux session and no transcript
    assert rich["multiplexer"] == ""
    assert rich["pid"] == 0
    assert rich["ppid"] == 0
    assert rich["subagent_count"] == 0
    assert rich["context_pct"] == 0.0
    assert rich["current_tool"] == ""
    assert rich["last_activity"] == ""
    assert rich["started_at_transcript"] == ""
    assert rich["model_transcript"] == ""
    # Skills parsed from CLAUDE.md
    assert rich["skills_loaded"] == [
        "scitex",
        "scitex-orochi",
        "scitex-agent-container",
    ]
    assert rich["workdir"] == str(fake_workspace)
    assert rich["project"] == "fake-agent"
    assert rich["machine"]  # hostname always populated


def test_encode_claude_project() -> None:
    # hidden-dir: /.scitex becomes --scitex (not ---scitex)
    assert agent_meta._encode_claude_project(
        "/Users/ywatanabe/.dotfiles/src/.scitex/orochi/workspaces/head-mba"
    ) == "-Users-ywatanabe--dotfiles-src--scitex-orochi-workspaces-head-mba"


def test_collect_rich_with_fake_transcript(
    fake_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a fake ~/.claude/projects/<encoded>/*.jsonl layout under tmp_path
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Also patch Path.home() which caches nothing
    monkeypatch.setattr(Path, "home", lambda: home)

    resolved = str(fake_workspace.resolve())
    encoded = agent_meta._encode_claude_project(resolved)
    proj = home / ".claude" / "projects" / encoded
    proj.mkdir(parents=True)

    jsonl = proj / "session.jsonl"
    lines = [
        {"type": "user", "message": {"content": "hi"}},
        {
            "type": "assistant",
            "timestamp": "2026-04-12T12:00:00Z",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 1000,
                    "cache_read_input_tokens": 499000,
                    "cache_creation_input_tokens": 0,
                },
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ],
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(x) for x in lines))

    with patch.object(agent_meta, "detect_multiplexer", return_value=""):
        rich = agent_meta.collect_rich(
            name="fake-agent",
            workdir=str(fake_workspace),
            session="fake-agent",
        )

    assert rich["context_pct"] == 50.0
    assert rich["current_tool"] == "Bash"
    assert rich["last_activity"] == "2026-04-12T12:00:00Z"
    assert rich["model_transcript"] == "claude-opus-4-6"
    assert rich["started_at_transcript"]  # ISO UTC timestamp


# ---------------------------------------------------------------------------
# agent_status — integration: rich fields merged into base result
# ---------------------------------------------------------------------------


def test_agent_status_includes_rich_fields(
    fake_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scitex_agent_container import lifecycle

    class _FakeEntry(dict):
        pass

    entry = _FakeEntry(
        config="/nonexistent/fake.yaml",
        screen="fake-agent",
        started_at="2026-04-12T00:00:00Z",
    )

    class _FakeRegistry:
        def get(self, name):  # noqa: D401
            return entry

    # load_config will fail on /nonexistent — lifecycle catches that and
    # sets config=None. That path still calls collect_rich via the
    # fallback workspace dir, so point HOME at the fake workspace parent.
    monkeypatch.setattr(Path, "home", lambda: fake_workspace.parent.parent)
    # The fallback workdir lifecycle computes:
    #   ~/.scitex/orochi/workspaces/<name>
    # so mirror that:
    target = fake_workspace.parent.parent / ".scitex" / "orochi" / "workspaces"
    target.mkdir(parents=True, exist_ok=True)
    link = target / "fake-agent"
    if not link.exists():
        link.symlink_to(fake_workspace)

    with patch.object(agent_meta, "detect_multiplexer", return_value=""):
        result = lifecycle.agent_status("fake-agent", registry=_FakeRegistry())

    # Base fields
    assert result["name"] == "fake-agent"
    assert result["status"] == "stopped"
    # Rich fields merged in
    assert "multiplexer" in result
    assert "skills_loaded" in result
    assert "context_pct" in result
    assert "machine" in result
    assert result["skills_loaded"] == [
        "scitex",
        "scitex-orochi",
        "scitex-agent-container",
    ]


# ---------------------------------------------------------------------------
# --terse projection (todo#300)
# ---------------------------------------------------------------------------


def test_status_terse_emits_only_whitelisted_fields() -> None:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {
        "agent": "a1",
        "state": "running",
        "timestamp": "2026-04-13T00:00:00Z",
        "tmux_alive": True,
        "last_post_ts": "2026-04-13T00:00:00Z",
        "context_management": {
            "percent": 42.0,
            "strategy": "compact",
            "trigger_at_percent": 85,
        },
        "pids": {"claude_code": 1234, "container_daemon": 5678, "extra": 9},
        "health": {"ok": True, "details": "xyz"},
        "snapshot": {
            "timestamp": "2026-04-13T00:00:00Z",
            "has_diff": False,
            "diff_fields": ["tmux_count"],  # must NOT leak into terse
        },
        "extra_bulky_field": "x" * 5000,  # must NOT leak
        "agent_meta": {"context_pct": 42.0},  # must NOT leak
    }
    terse = project_terse(full, TERSE_STATUS_FIELDS)
    assert set(terse.keys()) == set(TERSE_STATUS_FIELDS)
    assert terse["agent"] == "a1"
    assert terse["context_management.percent"] == 42.0
    assert terse["pids.claude_code"] == 1234
    assert terse["health.ok"] is True
    assert terse["snapshot.has_diff"] is False
    assert "extra_bulky_field" not in terse
    assert "diff_fields" not in terse
    # Also: no dotted key like "snapshot.diff_fields" should appear
    for k in terse:
        assert "diff_fields" not in k


def test_status_terse_absent_fields_emit_null() -> None:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    # Source lacks context_management entirely + lacks pids + lacks health
    full = {"agent": "ghost", "state": "stopped"}
    terse = project_terse(full, TERSE_STATUS_FIELDS)
    assert terse["context_management.percent"] is None
    assert terse["context_management.strategy"] is None
    assert terse["pids.claude_code"] is None
    assert terse["pids.container_daemon"] is None
    assert terse["health.ok"] is None
    assert terse["snapshot.timestamp"] is None
    # Shape is stable: every whitelist key is present
    assert set(terse.keys()) == set(TERSE_STATUS_FIELDS)


def test_status_terse_context_management_null_when_disabled() -> None:
    """Regression: context_management may be ``None`` in real agent_status."""
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {"agent": "a2", "context_management": None}
    terse = project_terse(full, TERSE_STATUS_FIELDS)
    assert terse["context_management.percent"] is None
    assert terse["context_management.strategy"] is None


def test_status_full_unaffected_by_terse_flag_absence(
    fake_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default status (no --terse) still emits every rich field."""
    from scitex_agent_container import lifecycle

    class _FakeEntry(dict):
        pass

    entry = _FakeEntry(
        config="/nonexistent/fake.yaml",
        screen="fake-agent",
        started_at="2026-04-12T00:00:00Z",
    )

    class _FakeRegistry:
        def get(self, name):
            return entry

    monkeypatch.setattr(Path, "home", lambda: fake_workspace.parent.parent)
    target = fake_workspace.parent.parent / ".scitex" / "orochi" / "workspaces"
    target.mkdir(parents=True, exist_ok=True)
    link = target / "fake-agent"
    if not link.exists():
        link.symlink_to(fake_workspace)

    with patch.object(agent_meta, "detect_multiplexer", return_value=""):
        result = lifecycle.agent_status("fake-agent", registry=_FakeRegistry())

    # Full rich field set is still emitted (regression guard for terse
    # being accidentally applied to the default path).
    for key in ("skills_loaded", "hooks_configured", "listen", "extensions"):
        assert key in result
