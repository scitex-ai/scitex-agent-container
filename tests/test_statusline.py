"""Tests for the sac-statusline command (statusline.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.statusline import _persist, read_statusline_json


def test_read_statusline_json_missing(tmp_path, monkeypatch):
    """Returns None when the statusline JSON file does not exist."""
    import scitex_agent_container.statusline as sl_mod

    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)
    assert read_statusline_json("no-such-agent") is None


def test_persist_and_read(tmp_path, monkeypatch):
    """Persisting a payload and reading it back returns identical data."""
    import scitex_agent_container.statusline as sl_mod

    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)

    payload = {
        "context_window": {"used_percentage": 42.5},
        "model": {"display_name": "claude-sonnet-4-6"},
        "rate_limits": {
            "five_hour": {"used_percentage": 10.0, "resets_at": "2026-04-30T00:00:00Z"},
            "seven_day": {"used_percentage": 5.0, "resets_at": "2026-05-06T00:00:00Z"},
        },
    }
    raw = json.dumps(payload).encode()
    _persist(raw, "test-agent")

    result = read_statusline_json("test-agent")
    assert result is not None
    assert result["context_window"]["used_percentage"] == 42.5
    assert result["model"]["display_name"] == "claude-sonnet-4-6"


def test_persist_atomic(tmp_path, monkeypatch):
    """A second persist overwrites the previous file atomically (no .tmp left)."""
    import scitex_agent_container.statusline as sl_mod

    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)

    _persist(json.dumps({"v": 1}).encode(), "agent-x")
    _persist(json.dumps({"v": 2}).encode(), "agent-x")

    assert not (tmp_path / "agent-x.json.tmp").exists()
    data = json.loads((tmp_path / "agent-x.json").read_text())
    assert data["v"] == 2


def test_read_statusline_json_corrupt(tmp_path, monkeypatch):
    """Returns None gracefully when the file contains invalid JSON."""
    import scitex_agent_container.statusline as sl_mod

    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)
    (tmp_path / "bad-agent.json").write_text("not json")
    assert read_statusline_json("bad-agent") is None
