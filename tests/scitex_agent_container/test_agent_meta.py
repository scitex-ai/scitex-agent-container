"""Tests for the ``sdk_session`` field in ``agent_meta.collect_rich``.

Covers the read path that surfaces claude-session runtime state on the
status JSON so dashboards and ``sac show-status`` can render quota +
session id without poking at on-disk paths themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container import agent_meta
from scitex_agent_container._runners import claude_session as runner


@pytest.fixture
def isolated_runtime(monkeypatch, tmp_path):
    """Redirect the runner's default state root and chdir into a clean
    tmp_path so ``find_project_scope`` walks up from a dir without a
    repo marker — forcing collect_rich to fall through to the
    home-scope state_dir, which we've redirected here."""
    monkeypatch.setattr(runner, "DEFAULT_STATE_ROOT", tmp_path)
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
