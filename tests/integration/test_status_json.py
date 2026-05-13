"""Tests for rich metadata collection in ``status --json``."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scitex_agent_container._state import agent_meta

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
    assert (
        agent_meta._encode_claude_project(
            "/Users/ywatanabe/.dotfiles/src/.scitex/orochi/workspaces/head-mba"
        )
        == "-Users-ywatanabe--dotfiles-src--scitex-orochi-workspaces-head-mba"
    )


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
# parse_subagent_count_from_pane_text — regex pinned across marker variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pane,expected",
    [
        # Canonical: "N local agent(s) running"
        ("  ✶ 1 local agent running · 2s\n❯ ", 1),
        ("  ✶ 3 local agents running · 12s\n", 3),
        # "still running" variant (singular + plural).
        ("  ✢ 1 local agent still running · 1m 4s\n", 1),
        ("  ✢ 5 local agents still running · 45s\n", 5),
        # Explicit zero is parsed (not treated as "no marker").
        ("  0 local agents running\n", 0),
        # No marker → 0.
        ("regular chat output\nnothing here\n❯ ", 0),
        # Empty / None → 0 (None guarded by the caller via ``or ""``).
        ("", 0),
        # Chat prose that merely mentions "local agent" must NOT
        # false-positive — the regex anchors on the ``running`` trailer.
        ("reviewing 2 local agent names that were stale last cycle\n", 0),
    ],
)
def test_parse_subagent_count_from_pane_text(pane: str, expected: int) -> None:
    assert agent_meta.parse_subagent_count_from_pane_text(pane) == expected


# ---------------------------------------------------------------------------
# _fallback_workdir — sac's own workspace root
# ---------------------------------------------------------------------------


def test_fallback_workdir_uses_sac_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returns ~/.scitex/agent-container/runtime/agents/<id>."""
    from scitex_agent_container._lifecycle.lifecycle import _fallback_workdir

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = _fallback_workdir("some-agent")
    assert result == str(
        tmp_path / ".scitex" / "agent-container" / "runtime" / "agents" / "some-agent"
    )


# ---------------------------------------------------------------------------
# agent_status — integration: rich fields merged into base result
# ---------------------------------------------------------------------------


def test_agent_status_includes_rich_fields(
    fake_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scitex_agent_container._lifecycle import lifecycle

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
    #   ~/.scitex/agent-container/runtime/agents/<name>
    target = (
        fake_workspace.parent.parent
        / ".scitex"
        / "agent-container"
        / "runtime"
        / "agents"
    )
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


def test_terse_status_is_heartbeat_safe() -> None:
    """lead msg#16005 contract: scitex-orochi's heartbeat pusher shells
    out to ``scitex-agent-container show-status <name> --terse --json`` and
    forwards the parsed dict verbatim as ``sac_status`` on
    ``POST /api/agents/register/``.

    Pin the three invariants that pivot relies on:

    1. ``--terse`` output is a *flat* dict — all keys are strings,
       values are JSON-primitives (str / int / float / bool / None).
       A nested-dict leak would explode the hub registry's storage
       shape and break the forward-new-fields-automatically promise.

    2. Every whitelisted key is present (``None`` for missing source
       fields) — consumers can read any ``TERSE_STATUS_FIELDS``
       entry without an ``in`` check.

    3. The payload is small (< 4 KB) — heartbeats run every 30 s
       across the fleet, so the ~18x reduction vs full ``--json``
       is load-bearing.
    """
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    # Simulate what ``agent_status`` hands to ``project_terse`` with a
    # realistic full payload (context_management nested, pids nested,
    # snapshot block present, plus noise fields).
    realistic = {
        "agent": "worker-mba",
        "state": "running",
        "timestamp": "2026-04-20T01:23:45Z",
        "tmux_alive": True,
        "last_post_ts": "2026-04-20T01:23:30Z",
        "context_management": {
            "percent": 37.5,
            "strategy": "compact",
            "trigger_at_percent": 85,
        },
        "pids": {"claude_code": 12345, "container_daemon": 23456},
        "health": {"ok": True, "details": "fresh"},
        "snapshot": {
            "timestamp": "2026-04-20T01:23:30Z",
            "has_diff": False,
            "diff_fields": [],  # must NOT leak — diff_fields is noisy
        },
        "agent_meta": {"context_pct": 37.5},  # noise — must NOT leak
        "pane_text": "x" * 10000,  # noise — must NOT leak
    }
    terse = project_terse(realistic, TERSE_STATUS_FIELDS)

    # (1) Flat dict, JSON-primitive leaves only.
    for k, v in terse.items():
        assert isinstance(k, str), f"non-string key {k!r}"
        assert v is None or isinstance(v, (str, int, float, bool)), (
            f"non-primitive value for {k}: {type(v).__name__}"
        )

    # (2) Every whitelist key present.
    assert set(terse.keys()) == set(TERSE_STATUS_FIELDS)

    # (3) Small payload — round-trips under 4 KB.
    assert len(json.dumps(terse)) < 4096


def test_status_terse_whitelist_includes_extended_fields() -> None:
    """todo#300 follow-up: whitelist extends beyond the original 13 fields.

    The heartbeat path (PR #66 pivot) depends on these names being
    present in the terse projection.
    """
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Original 13 (unchanged; kept as a regression guard)
    original_13 = {
        "agent",
        "state",
        "timestamp",
        "tmux_alive",
        "last_post_ts",
        "context_management.percent",
        "context_management.strategy",
        "context_management.trigger_at_percent",
        "pids.claude_code",
        "pids.container_daemon",
        "health.ok",
        "snapshot.timestamp",
        "snapshot.has_diff",
    }
    assert original_13.issubset(set(TERSE_STATUS_FIELDS))

    # Extended tranche — high-value heartbeat fields
    extended = {
        "subagent_count",
        "subagents",
        "context_pct",
        "quota_5h_used_pct",
        "quota_7d_used_pct",
        "quota_5h_reset_at",
        "quota_7d_reset_at",
        "pane_state",
        "last_action_at",
        "last_action_name",
        "last_action_outcome",
        "last_tool_at",
        "last_tool_name",
        "current_task",
        "current_tool",
        "account_email",
        "skills_loaded",
        "hostname_canonical",
        "machine",
    }
    assert extended.issubset(set(TERSE_STATUS_FIELDS)), (
        f"missing extended fields: {extended - set(TERSE_STATUS_FIELDS)}"
    )


def test_status_terse_excludes_pii_and_bulky_fields() -> None:
    """PII / heavy fields must stay full-mode-only (todo#300 follow-up).

    These fields may carry prompt fragments, full CLAUDE.md contents, or
    raw pane scrollback — they are deliberately excluded from the
    heartbeat-bound terse projection.
    """
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    pii_blacklist = {
        "pane_text",
        "claude_md",
        "mcp_json",
        "last_user_msg",
        "stuck_prompt_text",
        "recent_prompts",
        "current_tool_input",
        "recent_tools",
    }
    # Not in the whitelist tuple itself
    assert pii_blacklist.isdisjoint(set(TERSE_STATUS_FIELDS))

    # And not in the projected output either, even when present in source
    full = {name: f"SENSITIVE_{name}" * 200 for name in pii_blacklist}
    full["agent"] = "x"
    terse = project_terse(full, TERSE_STATUS_FIELDS)
    for name in pii_blacklist:
        assert name not in terse
        # Also ensure no residue leaked into any value
        for v in terse.values():
            if isinstance(v, str):
                assert "SENSITIVE_" not in v


def test_status_terse_payload_size_under_threshold() -> None:
    """Terse payload stays under 4 KB on a representative full snapshot.

    Lead's target was ~1-2 KB / ~32 fields. The 4 KB ceiling gives
    headroom for long ISO timestamps + skills lists without allowing
    the projection to silently bloat toward the 28 KB full size.
    """
    import json as _json

    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {
        # Original 13 sources
        "agent": "head-mba",
        "state": "running",
        "timestamp": "2026-04-20T12:34:56Z",
        "tmux_alive": True,
        "last_post_ts": "2026-04-20T12:34:00Z",
        "context_management": {
            "percent": 42.5,
            "strategy": "compact",
            "trigger_at_percent": 85,
        },
        "pids": {"claude_code": 12345, "container_daemon": 67890},
        "health": {"ok": True, "details": "everything nominal"},
        "snapshot": {
            "timestamp": "2026-04-20T12:30:00Z",
            "has_diff": False,
            "diff_fields": [],
        },
        # Extended tranche sources
        "subagent_count": 2,
        "subagents": 2,
        "context_pct": 42.5,
        "quota_5h_used_pct": 37.5,
        "quota_7d_used_pct": 61.2,
        "quota_5h_reset_at": "2026-04-20T17:00:00Z",
        "quota_7d_reset_at": "2026-04-27T00:00:00Z",
        "pane_state": "running",
        "last_action_at": "2026-04-20T12:30:00Z",
        "last_action_name": "nonce-probe",
        "last_action_outcome": "alive",
        "last_tool_at": "2026-04-20T12:34:00Z",
        "last_tool_name": "Bash",
        "current_task": "Bash: git status",
        "current_tool": "Bash",
        "account_email": "ywata1989@gmail.com",
        "skills_loaded": [
            "orochi-agent-startup-protocol",
            "orochi-fleet-communication-discipline",
            "orochi-fleet-members",
            "orochi-user-communication",
            "orochi-fleet-resurrection-protocol",
        ],
        "hostname_canonical": "mba.hpc.unimelb.edu.au",
        "machine": "mba",
        # Bulky / PII — must be dropped by the projection
        "pane_text": "x" * 10000,
        "claude_md": "y" * 20000,
        "mcp_json": "z" * 5000,
        "last_user_msg": "q" * 200,
        "stuck_prompt_text": "s" * 200,
        "recent_prompts": ["p" * 500] * 10,
        "current_tool_input": "c" * 120,
        "recent_tools": [{"tool": "Bash", "input": "x" * 300}] * 20,
    }
    projected = project_terse(full, TERSE_STATUS_FIELDS)
    size = len(_json.dumps(projected))
    assert size < 4096, f"terse payload too large: {size}B"
    # And the expected shape: 34 fields (added open_agent_calls_count +
    # oldest_open_agent_age_s for orochi#133 stuck-subagent detection)
    assert len(TERSE_STATUS_FIELDS) == 34
    assert set(projected.keys()) == set(TERSE_STATUS_FIELDS)


def test_status_full_unaffected_by_terse_flag_absence(
    fake_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default status (no --terse) still emits every rich field."""
    from scitex_agent_container._lifecycle import lifecycle

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
    target = fake_workspace.parent.parent / ".scitex" / "orochi" / "runtime" / "agents"
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
