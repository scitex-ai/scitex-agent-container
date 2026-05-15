"""Tests for rich metadata collection in ``status --json``.

TQ cleanup: every test below carries explicit Arrange / Act / Assert
markers (TQ002), spells out the behaviour in its name (TQ003), and
asserts exactly one fact (TQ007). Same-shape invariants (e.g. the
defaulted-zero fields of ``collect_rich`` when no tmux/transcript is
available) collapse into ``pytest.parametrize`` cases.

STX-NM cleanup: no ``unittest.mock``. ``detect_multiplexer`` is starved
of a tmux/screen session by installing fake binaries on PATH via the
shared ``subprocess_shim`` fixture; both fakes ``exit 1`` so the
function falls through to its empty-string return. ``Path.home()`` is
redirected by swapping ``$HOME`` (which is what ``Path.home`` reads on
POSIX) through ``env_save_restore`` — no monkeypatching of stdlib.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._state import agent_meta

# ---------------------------------------------------------------------------
# Shared fixtures
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


@pytest.fixture
def no_multiplexer(subprocess_shim):
    """Honest replacement for ``patch.object(detect_multiplexer, ...)``.

    Installs fake ``tmux`` and ``screen`` binaries on PATH that exit
    non-zero / print nothing, so ``detect_multiplexer`` returns ``""``.
    """
    subprocess_shim.install("tmux", exit=1)
    subprocess_shim.install("screen", exit=0, stdout="")
    return subprocess_shim


# ---------------------------------------------------------------------------
# collect_rich — shape & defaults (parametrized one-assert-each)
# ---------------------------------------------------------------------------


REQUIRED_KEYS = [
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
]


@pytest.fixture
def collected_rich_defaults(fake_workspace: Path, no_multiplexer):
    return agent_meta.collect_rich(
        name="fake-agent",
        workdir=str(fake_workspace),
        session="fake-agent",
    )


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_collect_rich_emits_required_key(
    collected_rich_defaults: dict, key: str
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert key in rich


DEFAULTS_WHEN_IDLE = [
    ("multiplexer", ""),
    ("pid", 0),
    ("ppid", 0),
    ("subagent_count", 0),
    ("context_pct", 0.0),
    ("current_tool", ""),
    ("last_activity", ""),
    ("started_at_transcript", ""),
    ("model_transcript", ""),
]


@pytest.mark.parametrize("key,expected", DEFAULTS_WHEN_IDLE)
def test_collect_rich_default_value_when_no_session_or_transcript(
    collected_rich_defaults: dict, key: str, expected
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert rich[key] == expected


def test_collect_rich_parses_skills_block_from_claude_md(
    collected_rich_defaults: dict,
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert rich["skills_loaded"] == [
        "scitex",
        "scitex-orochi",
        "scitex-agent-container",
    ]


def test_collect_rich_records_workdir_verbatim(
    collected_rich_defaults: dict, fake_workspace: Path
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert rich["workdir"] == str(fake_workspace)


def test_collect_rich_uses_workspace_basename_as_project(
    collected_rich_defaults: dict,
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert rich["project"] == "fake-agent"


def test_collect_rich_populates_machine_with_hostname(
    collected_rich_defaults: dict,
) -> None:
    # Arrange
    rich = collected_rich_defaults
    # Act — already collected by fixture.
    # Assert
    assert rich["machine"]


# ---------------------------------------------------------------------------
# _encode_claude_project
# ---------------------------------------------------------------------------


def test_encode_claude_project_collapses_hidden_dir_to_double_dash() -> None:
    # Arrange
    workdir = "/Users/ywatanabe/.dotfiles/src/.scitex/orochi/workspaces/head-mba"
    # Act
    encoded = agent_meta._encode_claude_project(workdir)
    # Assert
    assert encoded == (
        "-Users-ywatanabe--dotfiles-src--scitex-orochi-workspaces-head-mba"
    )


# ---------------------------------------------------------------------------
# collect_rich — with a fake transcript jsonl on disk
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_with_transcript(
    fake_workspace: Path,
    tmp_path: Path,
    env_save_restore,
    no_multiplexer,
) -> dict:
    # Build a fake ~/.claude/projects/<encoded>/*.jsonl layout under tmp_path.
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))

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

    return agent_meta.collect_rich(
        name="fake-agent",
        workdir=str(fake_workspace),
        session="fake-agent",
    )


def test_collect_rich_with_transcript_computes_context_pct(
    rich_with_transcript: dict,
) -> None:
    # Arrange — fixture builds the fake transcript.
    # Act — fixture collected rich.
    # Assert
    assert rich_with_transcript["context_pct"] == 50.0


def test_collect_rich_with_transcript_reports_current_tool(
    rich_with_transcript: dict,
) -> None:
    # Arrange — fixture builds the fake transcript.
    # Act — fixture collected rich.
    # Assert
    assert rich_with_transcript["current_tool"] == "Bash"


def test_collect_rich_with_transcript_reports_last_activity_timestamp(
    rich_with_transcript: dict,
) -> None:
    # Arrange — fixture builds the fake transcript.
    # Act — fixture collected rich.
    # Assert
    assert rich_with_transcript["last_activity"] == "2026-04-12T12:00:00Z"


def test_collect_rich_with_transcript_reports_model_from_assistant_message(
    rich_with_transcript: dict,
) -> None:
    # Arrange — fixture builds the fake transcript.
    # Act — fixture collected rich.
    # Assert
    assert rich_with_transcript["model_transcript"] == "claude-opus-4-6"


def test_collect_rich_with_transcript_emits_started_at_timestamp(
    rich_with_transcript: dict,
) -> None:
    # Arrange — fixture builds the fake transcript.
    # Act — fixture collected rich.
    # Assert
    assert rich_with_transcript["started_at_transcript"]


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
    # Arrange — input table.
    # Act
    actual = agent_meta.parse_subagent_count_from_pane_text(pane)
    # Assert
    assert actual == expected


# ---------------------------------------------------------------------------
# _fallback_workdir — sac's own workspace root
# ---------------------------------------------------------------------------


def test_fallback_workdir_uses_sac_workspace_root(
    tmp_path: Path, env_save_restore
) -> None:
    """Returns ~/.scitex/agent-container/runtime/agents/<id>."""
    # Arrange
    from scitex_agent_container._lifecycle.lifecycle import _fallback_workdir

    env_save_restore.set("HOME", str(tmp_path))
    # Act
    result = _fallback_workdir("some-agent")
    # Assert
    assert result == str(
        tmp_path / ".scitex" / "agent-container" / "runtime" / "agents" / "some-agent"
    )


# ---------------------------------------------------------------------------
# agent_status — integration: rich fields merged into base result
# ---------------------------------------------------------------------------


class _FakeEntry(dict):
    pass


class _FakeRegistry:
    def __init__(self, entry: dict) -> None:
        self._entry = entry

    def get(self, name):  # noqa: D401
        return self._entry


@pytest.fixture
def status_result(fake_workspace: Path, env_save_restore, no_multiplexer) -> dict:
    """Run ``lifecycle.agent_status`` against a fake registry + workspace.

    Uses real $HOME swap (no monkeypatch of ``Path.home``). The lifecycle
    fallback resolves ``~/.scitex/agent-container/runtime/agents/fake-agent``;
    we point that path at ``fake_workspace`` via a real symlink so
    ``collect_rich`` reads the CLAUDE.md we control.
    """
    from scitex_agent_container._lifecycle import lifecycle

    fake_home = fake_workspace.parent.parent
    env_save_restore.set("HOME", str(fake_home))

    target = fake_home / ".scitex" / "agent-container" / "runtime" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    link = target / "fake-agent"
    if not link.exists():
        link.symlink_to(fake_workspace)

    entry = _FakeEntry(
        config="/nonexistent/fake.yaml",
        screen="fake-agent",
        started_at="2026-04-12T00:00:00Z",
    )
    return lifecycle.agent_status("fake-agent", registry=_FakeRegistry(entry))


def test_agent_status_returns_requested_name(status_result: dict) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert status_result["name"] == "fake-agent"


def test_agent_status_reports_stopped_when_config_unloadable(
    status_result: dict,
) -> None:
    # Arrange — fixture built status_result with /nonexistent config path.
    # Act — fixture already invoked agent_status.
    # Assert
    assert status_result["status"] == "stopped"


@pytest.mark.parametrize(
    "key", ["multiplexer", "skills_loaded", "context_pct", "machine"]
)
def test_agent_status_merges_rich_field(status_result: dict, key: str) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert key in status_result


def test_agent_status_includes_parsed_skills_loaded(status_result: dict) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert status_result["skills_loaded"] == [
        "scitex",
        "scitex-orochi",
        "scitex-agent-container",
    ]


# ---------------------------------------------------------------------------
# --terse projection (todo#300)
# ---------------------------------------------------------------------------


FULL_TERSE_SOURCE = {
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


@pytest.fixture
def terse_from_full() -> dict:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    return project_terse(FULL_TERSE_SOURCE, TERSE_STATUS_FIELDS)


def test_terse_keyset_matches_whitelist(terse_from_full: dict) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — fixture builds.
    # Assert
    assert set(terse_from_full.keys()) == set(TERSE_STATUS_FIELDS)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("agent", "a1"),
        ("context_management.percent", 42.0),
        ("pids.claude_code", 1234),
        ("health.ok", True),
        ("snapshot.has_diff", False),
    ],
)
def test_terse_projects_dotted_field(terse_from_full: dict, key: str, expected) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert terse_from_full[key] == expected


@pytest.mark.parametrize("noisy", ["extra_bulky_field", "diff_fields"])
def test_terse_drops_noisy_top_level_field(terse_from_full: dict, noisy: str) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert noisy not in terse_from_full


def test_terse_does_not_emit_dotted_diff_fields_key(
    terse_from_full: dict,
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert not any("diff_fields" in k for k in terse_from_full)


# --- Absent-fields-emit-null behaviour ---


@pytest.fixture
def terse_from_minimal() -> dict:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {"agent": "ghost", "state": "stopped"}
    return project_terse(full, TERSE_STATUS_FIELDS)


@pytest.mark.parametrize(
    "key",
    [
        "context_management.percent",
        "context_management.strategy",
        "pids.claude_code",
        "pids.container_daemon",
        "health.ok",
        "snapshot.timestamp",
    ],
)
def test_terse_missing_source_field_projects_as_null(
    terse_from_minimal: dict, key: str
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert terse_from_minimal[key] is None


def test_terse_shape_is_stable_when_source_is_sparse(
    terse_from_minimal: dict,
) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — fixture builds.
    # Assert
    assert set(terse_from_minimal.keys()) == set(TERSE_STATUS_FIELDS)


# --- context_management may be ``None`` (regression) ---


@pytest.mark.parametrize(
    "key", ["context_management.percent", "context_management.strategy"]
)
def test_terse_handles_context_management_set_to_none(key: str) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {"agent": "a2", "context_management": None}
    # Act
    terse = project_terse(full, TERSE_STATUS_FIELDS)
    # Assert
    assert terse[key] is None


# --- Heartbeat invariants (lead msg#16005) ---


@pytest.fixture
def realistic_terse() -> dict:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

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
            "diff_fields": [],
        },
        "agent_meta": {"context_pct": 37.5},
        "pane_text": "x" * 10000,
    }
    return project_terse(realistic, TERSE_STATUS_FIELDS)


def test_heartbeat_terse_keys_are_all_strings(realistic_terse: dict) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert all(isinstance(k, str) for k in realistic_terse)


def test_heartbeat_terse_values_are_json_primitives(
    realistic_terse: dict,
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert all(
        v is None or isinstance(v, (str, int, float, bool))
        for v in realistic_terse.values()
    )


def test_heartbeat_terse_covers_every_whitelisted_key(
    realistic_terse: dict,
) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — fixture builds.
    # Assert
    assert set(realistic_terse.keys()) == set(TERSE_STATUS_FIELDS)


def test_heartbeat_terse_payload_round_trips_under_4kb(
    realistic_terse: dict,
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert len(json.dumps(realistic_terse)) < 4096


# --- Whitelist composition ---


ORIGINAL_13 = [
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
]


EXTENDED_FIELDS = [
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
]


@pytest.mark.parametrize("field", ORIGINAL_13)
def test_terse_whitelist_keeps_original_field(field: str) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — import.
    # Assert
    assert field in TERSE_STATUS_FIELDS


@pytest.mark.parametrize("field", EXTENDED_FIELDS)
def test_terse_whitelist_includes_extended_field(field: str) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — import.
    # Assert
    assert field in TERSE_STATUS_FIELDS


# --- PII / bulky blacklist ---


PII_BLACKLIST = [
    "pane_text",
    "claude_md",
    "mcp_json",
    "last_user_msg",
    "stuck_prompt_text",
    "recent_prompts",
    "current_tool_input",
    "recent_tools",
]


@pytest.mark.parametrize("name", PII_BLACKLIST)
def test_terse_whitelist_excludes_pii_field(name: str) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — import.
    # Assert
    assert name not in TERSE_STATUS_FIELDS


@pytest.fixture
def terse_with_pii_source() -> dict:
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    full = {name: f"SENSITIVE_{name}" * 200 for name in PII_BLACKLIST}
    full["agent"] = "x"
    return project_terse(full, TERSE_STATUS_FIELDS)


@pytest.mark.parametrize("name", PII_BLACKLIST)
def test_terse_projection_drops_pii_field_when_present_in_source(
    terse_with_pii_source: dict, name: str
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert name not in terse_with_pii_source


def test_terse_projection_does_not_leak_pii_marker_into_any_value(
    terse_with_pii_source: dict,
) -> None:
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert not any(
        isinstance(v, str) and "SENSITIVE_" in v for v in terse_with_pii_source.values()
    )


# --- Payload size on a full representative snapshot ---


@pytest.fixture
def representative_full_status() -> dict:
    return {
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


def test_terse_projected_size_stays_under_4kb(
    representative_full_status: dict,
) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    # Act
    projected = project_terse(representative_full_status, TERSE_STATUS_FIELDS)
    # Assert
    assert len(json.dumps(projected)) < 4096


def test_terse_whitelist_pins_34_field_contract() -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS

    # Act — import.
    # Assert
    assert len(TERSE_STATUS_FIELDS) == 34


def test_terse_projection_covers_whitelist_on_representative_snapshot(
    representative_full_status: dict,
) -> None:
    # Arrange
    from scitex_agent_container.terse import TERSE_STATUS_FIELDS, project_terse

    # Act
    projected = project_terse(representative_full_status, TERSE_STATUS_FIELDS)
    # Assert
    assert set(projected.keys()) == set(TERSE_STATUS_FIELDS)


# ---------------------------------------------------------------------------
# Full path is unaffected by --terse absence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["skills_loaded", "hooks_configured", "listen", "extensions"]
)
def test_default_status_still_emits_full_rich_key(
    status_result: dict, key: str
) -> None:
    """Regression guard for the terse path being accidentally applied to
    the default ``agent_status`` output. Re-uses the integration
    ``status_result`` fixture, which runs ``agent_status`` with no
    terse projection.
    """
    # Arrange — fixture built status_result.
    # Act — fixture already invoked agent_status.
    # Assert
    assert key in status_result
