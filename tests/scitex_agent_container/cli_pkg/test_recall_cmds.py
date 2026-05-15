"""Tests for cli_pkg.recall_cmds.

Drives the recall command via CliRunner; covers the _resolve_jsonl
multi-source resolution logic (direct path / session id glob / agent
name with registry / agent name falling back to workdir-slug glob)
plus the recall click command's filter+output paths.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.recall_cmds import (
    _parse_iso,
    _resolve_jsonl,
    recall,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path):
    """PA-306: $HOME save/restore — Path.home() reads $HOME on Unix."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@contextmanager
def _fake_registry(get_returns: Any) -> Iterator[None]:
    """Inject a fake Registry class into the registry module's namespace.

    Replaces ``patch("...registry.Registry")`` — same effect via direct
    attribute save/restore, no ``unittest.mock``.
    """
    import scitex_agent_container._state.registry as reg_mod

    class _FakeRegistry:
        def get(self, _name):
            if isinstance(get_returns, Exception):
                raise get_returns
            return get_returns

    if isinstance(get_returns, type) and issubclass(get_returns, Exception):
        # Caller wants Registry() itself to raise
        def _raise(*_a, **_kw):
            raise get_returns("registry broken")

        saved = reg_mod.Registry
        reg_mod.Registry = _raise  # type: ignore[assignment]
    else:
        saved = reg_mod.Registry
        reg_mod.Registry = _FakeRegistry  # type: ignore[assignment]
    try:
        yield
    finally:
        reg_mod.Registry = saved  # type: ignore[assignment]


@contextmanager
def _fake_load_config(payload: Any) -> Iterator[None]:
    """Swap scitex_agent_container.config.load_config for a fake."""
    import scitex_agent_container.config as cfg_mod

    saved = cfg_mod.load_config

    def _fn(*_a, **_kw):
        if isinstance(payload, Exception):
            raise payload
        return payload

    cfg_mod.load_config = _fn  # type: ignore[assignment]
    try:
        yield
    finally:
        cfg_mod.load_config = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_z_suffix_returns_utc_datetime():
    # Arrange
    raw = "2026-04-28T10:00:00Z"
    # Act
    dt = _parse_iso(raw)
    # Assert
    assert dt == datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


def test_parse_iso_naive_input_defaults_to_utc():
    # Arrange
    raw = "2026-04-28T10:00:00"
    # Act
    dt = _parse_iso(raw)
    # Assert
    assert dt.tzinfo == timezone.utc


def test_parse_iso_invalid_string_raises_click_exception():
    # Arrange
    raw = "not-a-date"
    # Act
    ctx = pytest.raises(click.ClickException)
    # Assert
    with ctx:
        _parse_iso(raw)


# ---------------------------------------------------------------------------
# _resolve_jsonl
# ---------------------------------------------------------------------------


def test_resolve_jsonl_direct_path_returns_same_path(tmp_path):
    # Arrange
    p = tmp_path / "x.jsonl"
    p.write_text("")
    # Act
    resolved = _resolve_jsonl(str(p))
    # Assert
    assert resolved == p


def test_resolve_jsonl_session_id_finds_unique_match(tmp_path):
    # Arrange
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    target = proj / "abc-123.jsonl"
    target.write_text("")
    # Act
    out = _resolve_jsonl("abc-123")
    # Assert
    assert out == target


def test_resolve_jsonl_session_id_ambiguous_raises_click_exception(tmp_path):
    # Arrange
    proj1 = tmp_path / ".claude" / "projects" / "a"
    proj2 = tmp_path / ".claude" / "projects" / "b"
    proj1.mkdir(parents=True)
    proj2.mkdir(parents=True)
    (proj1 / "sid.jsonl").write_text("")
    (proj2 / "sid.jsonl").write_text("")
    # Act
    ctx = pytest.raises(click.ClickException, match="Ambiguous")
    # Assert
    with ctx:
        _resolve_jsonl("sid")


def test_resolve_jsonl_unknown_token_raises_click_exception(tmp_path):
    # Arrange
    token = "ghost-session"
    # Act
    ctx = pytest.raises(click.ClickException)
    # Assert
    with ctx:
        _resolve_jsonl(token)


def test_resolve_jsonl_via_registry_session_id_file_resolves(tmp_path):
    # Arrange
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    target = proj / "the-sid.jsonl"
    target.write_text("")
    sid_dir = tmp_path / ".scitex" / "agent-container" / "runtime" / "agent1" / "agent1"
    sid_dir.mkdir(parents=True)
    (sid_dir / "session_id").write_text("the-sid\n")
    # Act
    with _fake_registry({"config": "/fake/agent1.yaml"}):
        out = _resolve_jsonl("agent1")
    # Assert
    assert out == target


def test_resolve_jsonl_via_registry_workdir_slug_picks_newest(tmp_path):
    """Registry entry exists, no session_id file. Falls back to projects/-<slug> glob."""
    # Arrange
    slug = "work-dir"  # from "/work/dir"
    proj = tmp_path / ".claude" / "projects" / f"-{slug}"
    proj.mkdir(parents=True)
    j1 = proj / "old.jsonl"
    j1.write_text("")
    j1_stat = j1.stat()
    j2 = proj / "new.jsonl"
    j2.write_text("")
    os.utime(j2, (j1_stat.st_mtime + 100, j1_stat.st_mtime + 100))

    class FakeCfg:
        expanded_workdir = "/work/dir"

    # Act
    with _fake_registry({"config": "/fake/agent2.yaml"}), _fake_load_config(FakeCfg()):
        out = _resolve_jsonl("agent2")
    # Assert
    assert out == j2


def test_resolve_jsonl_load_config_failure_raises_click_exception(tmp_path):
    """Registry entry exists, no session_id, load_config raises -> unresolved."""
    # Arrange
    registry_payload = {"config": "/fake/x.yaml"}
    cfg_error = ValueError("bad yaml")
    # Act
    ctx = pytest.raises(click.ClickException)
    # Assert
    with _fake_registry(registry_payload), _fake_load_config(cfg_error), ctx:
        _resolve_jsonl("agent3")


def test_resolve_jsonl_registry_construction_failure_raises_click_exception(
    tmp_path,
):
    """Registry() construction raises -> entry=None -> exception."""
    # Arrange
    registry_ctor_error = RuntimeError
    # Act
    ctx = pytest.raises(click.ClickException)
    # Assert
    with _fake_registry(registry_ctor_error), ctx:
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


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def two_record_log(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "hi human"),
            _assistant("2026-04-28T10:01:00Z", "hello reply"),
        ],
    )
    return p


def test_recall_stats_flag_exits_successfully(runner, two_record_log):
    # Arrange
    args = [str(two_record_log), "--stats"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0, result.output


def test_recall_stats_flag_emits_stats_header(runner, two_record_log):
    # Arrange
    args = [str(two_record_log), "--stats"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "# stats" in result.output


def test_recall_stats_flag_suppresses_entries_section(runner, two_record_log):
    # Arrange
    args = [str(two_record_log), "--stats"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "# entries" not in result.output


def test_recall_default_invocation_exits_successfully(runner, two_record_log):
    # Arrange
    args = [str(two_record_log)]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


def test_recall_default_invocation_emits_entries_header(runner, two_record_log):
    # Arrange
    args = [str(two_record_log)]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "# entries" in result.output


@pytest.mark.parametrize("expected", ["hi human", "hello reply"])
def test_recall_default_invocation_includes_message_text(
    runner, two_record_log, expected
):
    # Arrange
    args = [str(two_record_log)]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert expected in result.output


@pytest.fixture
def role_filter_log(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "first user"),
            _user("2026-04-28T10:01:00Z", "second one"),
            _assistant("2026-04-28T10:02:00Z", "reply"),
        ],
    )
    return p


def test_recall_role_and_contains_filter_exits_successfully(runner, role_filter_log):
    # Arrange
    args = [str(role_filter_log), "--role", "user", "--contains", "second"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


def test_recall_role_and_contains_filter_keeps_matching_user(runner, role_filter_log):
    # Arrange
    args = [str(role_filter_log), "--role", "user", "--contains", "second"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "second one" in result.output


def test_recall_role_filter_excludes_other_roles(runner, role_filter_log):
    # Arrange
    args = [str(role_filter_log), "--role", "user", "--contains", "second"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "reply" not in result.output


@pytest.fixture
def last_window_log(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2020-01-01T10:00:00Z", "old"),
            _user("2020-01-01T10:25:00Z", "near"),
            _user("2020-01-01T10:30:00Z", "end"),
        ],
    )
    return p


def test_recall_last_window_exits_successfully(runner, last_window_log):
    # Arrange
    args = [str(last_window_log), "--last", "10m"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


@pytest.mark.parametrize("expected", ["near", "end"])
def test_recall_last_window_keeps_recent_entries(runner, last_window_log, expected):
    # Arrange
    args = [str(last_window_log), "--last", "10m"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert expected in result.output


def test_recall_last_window_drops_pre_window_entries(runner, last_window_log):
    # Arrange
    args = [str(last_window_log), "--last", "10m"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "old" not in result.output


@pytest.fixture
def since_until_log(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            _user("2026-04-28T10:00:00Z", "early"),
            _user("2026-04-28T10:30:00Z", "middle"),
            _user("2026-04-28T11:00:00Z", "late"),
        ],
    )
    return p


SINCE_UNTIL_ARGS = [
    "--since",
    "2026-04-28T10:15:00Z",
    "--until",
    "2026-04-28T10:45:00Z",
]


def test_recall_since_until_exits_successfully(runner, since_until_log):
    # Arrange
    args = [str(since_until_log), *SINCE_UNTIL_ARGS]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


def test_recall_since_until_keeps_in_range_entry(runner, since_until_log):
    # Arrange
    args = [str(since_until_log), *SINCE_UNTIL_ARGS]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "middle" in result.output


def test_recall_since_until_drops_pre_window_entry(runner, since_until_log):
    # Arrange
    args = [str(since_until_log), *SINCE_UNTIL_ARGS]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "early" not in result.output


@pytest.fixture
def five_message_log(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [_user(f"2026-04-28T10:{i:02d}:00Z", f"msg{i}") for i in range(5)],
    )
    return p


def test_recall_limit_exits_successfully(runner, five_message_log):
    # Arrange
    args = [str(five_message_log), "--limit", "2"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


@pytest.mark.parametrize("expected", ["msg3", "msg4"])
def test_recall_limit_keeps_tail_entries(runner, five_message_log, expected):
    # Arrange
    args = [str(five_message_log), "--limit", "2"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert expected in result.output


def test_recall_limit_drops_pre_tail_entries(runner, five_message_log):
    # Arrange
    args = [str(five_message_log), "--limit", "2"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "msg0" not in result.output


@pytest.fixture
def long_text_log(tmp_path):
    p = tmp_path / "s.jsonl"
    long_text = "x" * 1_000
    _write_jsonl(p, [_user("2026-04-28T10:00:00Z", long_text)])
    return p, long_text


def test_recall_body_limit_zero_exits_successfully(runner, long_text_log):
    # Arrange
    p, _ = long_text_log
    args = [str(p), "--body-limit", "0"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


def test_recall_body_limit_zero_preserves_full_body(runner, long_text_log):
    # Arrange
    p, long_text = long_text_log
    args = [str(p), "--body-limit", "0"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert long_text in result.output


@pytest.fixture
def session_id_layout(tmp_path):
    proj = tmp_path / ".claude" / "projects" / "encoded"
    proj.mkdir(parents=True)
    p = proj / "the-sid.jsonl"
    _write_jsonl(p, [_user("2026-04-28T10:00:00Z", "by-id")])
    return p


def test_recall_session_id_token_exits_successfully(runner, session_id_layout):
    # Arrange
    args = ["the-sid", "--stats"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code == 0


def test_recall_session_id_token_emits_stats_header(runner, session_id_layout):
    # Arrange
    args = ["the-sid", "--stats"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "# stats" in result.output


def test_recall_unresolvable_token_exits_nonzero(runner):
    # Arrange
    args = ["ghost"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert result.exit_code != 0


def test_recall_unresolvable_token_emits_not_found_message(runner):
    # Arrange
    args = ["ghost"]
    # Act
    result = runner.invoke(recall, args)
    # Assert
    assert "jsonl not found" in result.output
