"""Tests for cli_pkg.recall_cmds.

Drives the recall command via CliRunner; covers the _resolve_jsonl
multi-source resolution logic (direct path / session id glob / agent
name with registry / agent name falling back to workdir-slug glob)
plus the recall click command's filter+output paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.recall_cmds import (
    _parse_iso,
    _resolve_jsonl,
    recall,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_z_suffix():
    dt = _parse_iso("2026-04-28T10:00:00Z")
    assert dt == datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def test_parse_iso_no_tz_defaults_utc():
    dt = _parse_iso("2026-04-28T10:00:00")
    assert dt.tzinfo == timezone.utc


def test_parse_iso_invalid():
    import click

    with pytest.raises(click.ClickException):
        _parse_iso("not-a-date")


# ---------------------------------------------------------------------------
# _resolve_jsonl
# ---------------------------------------------------------------------------


def test_resolve_direct_path(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("")
    assert _resolve_jsonl(str(p)) == p


def test_resolve_session_id_glob(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    target = proj / "abc-123.jsonl"
    target.write_text("")
    out = _resolve_jsonl("abc-123")
    assert out == target


def test_resolve_session_id_ambiguous(tmp_path):
    import click

    proj1 = tmp_path / ".claude" / "projects" / "a"
    proj2 = tmp_path / ".claude" / "projects" / "b"
    proj1.mkdir(parents=True)
    proj2.mkdir(parents=True)
    (proj1 / "sid.jsonl").write_text("")
    (proj2 / "sid.jsonl").write_text("")
    with pytest.raises(click.ClickException) as exc:
        _resolve_jsonl("sid")
    assert "Ambiguous" in str(exc.value)


def test_resolve_unknown_raises(tmp_path):
    import click

    with pytest.raises(click.ClickException):
        _resolve_jsonl("ghost-session")


def test_resolve_via_registry_session_id_file(tmp_path):
    # registry has 'agent1'; session_id file points to a sid that exists under projects.
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    target = proj / "the-sid.jsonl"
    target.write_text("")

    sid_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "agent1" / "agent1"
    sid_dir.mkdir(parents=True)
    (sid_dir / "session_id").write_text("the-sid\n")

    with patch("scitex_agent_container._state.registry.Registry") as RegCls:
        # The recall code does `from .._state.registry import Registry` then `Registry().get(arg)`.
        instance = RegCls.return_value
        instance.get.return_value = {"config": "/fake/agent1.yaml"}
        out = _resolve_jsonl("agent1")
    assert out == target


def test_resolve_via_registry_workdir_slug(tmp_path):
    """Registry entry exists, no session_id file. Falls back to projects/-<slug> glob."""
    sid_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "agent2" / "agent2"
    # No session_id file. But provide config that load_config will read.

    # Slug: "/work/dir" -> "work-dir"
    slug = "work-dir"
    proj = tmp_path / ".claude" / "projects" / f"-{slug}"
    proj.mkdir(parents=True)
    j1 = proj / "old.jsonl"
    j1.write_text("")
    j1_stat = j1.stat()
    j2 = proj / "new.jsonl"
    j2.write_text("")
    # Force j2 newer
    import os

    os.utime(j2, (j1_stat.st_mtime + 100, j1_stat.st_mtime + 100))

    class FakeCfg:
        expanded_workdir = "/work/dir"

    with (
        patch("scitex_agent_container._state.registry.Registry") as RegCls,
        patch(
            "scitex_agent_container.config.load_config",
            return_value=FakeCfg(),
        ),
    ):
        RegCls.return_value.get.return_value = {"config": "/fake/agent2.yaml"}
        out = _resolve_jsonl("agent2")
    assert out == j2


def test_resolve_via_registry_load_config_fails(tmp_path):
    """Registry entry exists, no session_id, load_config raises → unresolved → exception."""
    import click

    with (
        patch("scitex_agent_container._state.registry.Registry") as RegCls,
        patch(
            "scitex_agent_container.config.load_config",
            side_effect=ValueError("bad yaml"),
        ),
    ):
        RegCls.return_value.get.return_value = {"config": "/fake/x.yaml"}
        with pytest.raises(click.ClickException):
            _resolve_jsonl("agent3")


def test_resolve_registry_raises_falls_through(tmp_path):
    """Registry().get() raises → entry=None → exception."""
    import click

    with patch(
        "scitex_agent_container._state.registry.Registry",
        side_effect=RuntimeError("registry broken"),
    ):
        with pytest.raises(click.ClickException):
            _resolve_jsonl("nobody")


# ---------------------------------------------------------------------------
# recall click command
# ---------------------------------------------------------------------------


def _user(ts, text):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
        "sessionId": "sess-1",
        "cwd": "/wd",
    }


def _assistant(ts, text):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_jsonl(path, recs):
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def test_recall_stats_only(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "hi"),
            _assistant("2026-04-28T10:01:00Z", "hello"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(recall, [str(p), "--stats"])
    assert result.exit_code == 0, result.output
    assert "# stats" in result.output
    assert "# entries" not in result.output


def test_recall_entries_basic(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "hi human"),
            _assistant("2026-04-28T10:01:00Z", "hello reply"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(recall, [str(p)])
    assert result.exit_code == 0
    assert "# entries" in result.output
    assert "hi human" in result.output
    assert "hello reply" in result.output


def test_recall_role_filter_and_contains(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "first user"),
            _user("2026-04-28T10:01:00Z", "second one"),
            _assistant("2026-04-28T10:02:00Z", "reply"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(recall, [str(p), "--role", "user", "--contains", "second"])
    assert result.exit_code == 0
    assert "second one" in result.output
    assert "reply" not in result.output


def test_recall_last_window(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2020-01-01T10:00:00Z", "old"),
            _user("2020-01-01T10:25:00Z", "near"),
            _user("2020-01-01T10:30:00Z", "end"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(recall, [str(p), "--last", "10m"])
    assert result.exit_code == 0
    assert "near" in result.output
    assert "end" in result.output
    assert "old" not in result.output


def test_recall_since_until(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "early"),
            _user("2026-04-28T10:30:00Z", "middle"),
            _user("2026-04-28T11:00:00Z", "late"),
        ],
    )
    runner = CliRunner()
    result = runner.invoke(
        recall,
        [
            str(p),
            "--since",
            "2026-04-28T10:15:00Z",
            "--until",
            "2026-04-28T10:45:00Z",
        ],
    )
    assert result.exit_code == 0
    assert "middle" in result.output
    assert "early" not in result.output


def test_recall_limit_caps(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [_user(f"2026-04-28T10:{i:02d}:00Z", f"msg{i}") for i in range(5)],
    )
    runner = CliRunner()
    result = runner.invoke(recall, [str(p), "--limit", "2"])
    assert result.exit_code == 0
    assert "msg3" in result.output
    assert "msg4" in result.output
    assert "msg0" not in result.output


def test_recall_body_limit_zero_means_no_truncate(tmp_path):
    p = tmp_path / "s.jsonl"
    long_text = "x" * 1000
    _write_jsonl(p, [_user("2026-04-28T10:00:00Z", long_text)])
    runner = CliRunner()
    result = runner.invoke(recall, [str(p), "--body-limit", "0"])
    assert result.exit_code == 0
    assert long_text in result.output


def test_recall_resolves_via_session_id(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    p = proj / "the-sid.jsonl"
    _write_jsonl(p, [_user("2026-04-28T10:00:00Z", "by-id")])

    runner = CliRunner()
    result = runner.invoke(recall, ["the-sid", "--stats"])
    assert result.exit_code == 0
    assert "# stats" in result.output


def test_recall_unresolvable_errors(tmp_path):
    runner = CliRunner()
    result = runner.invoke(recall, ["ghost"])
    assert result.exit_code != 0
    assert "jsonl not found" in result.output
