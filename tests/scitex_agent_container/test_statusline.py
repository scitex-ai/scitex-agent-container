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


# ---------------------------------------------------------------------------
# Merged from test_statusline_main.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------

import io
import json

import pytest

import scitex_agent_container.statusline as sl_mod

# ---------------------------------------------------------------------------
# _agent_name
# ---------------------------------------------------------------------------


def test_agent_name_prefers_sac_env(monkeypatch):
    monkeypatch.setattr(sl_mod, "_sac_env", lambda key: "from-sac-env")
    monkeypatch.setenv("CLAUDE_AGENT_ID", "ignored")
    assert sl_mod._agent_name() == "from-sac-env"


def test_agent_name_falls_back_to_claude_agent_id(monkeypatch):
    monkeypatch.setattr(sl_mod, "_sac_env", lambda key: "")
    monkeypatch.setenv("CLAUDE_AGENT_ID", "claude-aid")
    assert sl_mod._agent_name() == "claude-aid"


def test_agent_name_unknown_when_no_source(monkeypatch):
    monkeypatch.setattr(sl_mod, "_sac_env", lambda key: "")
    monkeypatch.delenv("CLAUDE_AGENT_ID", raising=False)
    assert sl_mod._agent_name() == "unknown"


# ---------------------------------------------------------------------------
# _persist edge case — OSError swallowed (write to a path that can't exist)
# ---------------------------------------------------------------------------


def test_persist_swallows_oserror(monkeypatch, tmp_path):
    """If the rename fails, persist must not raise."""
    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", boom)
    # Should NOT raise.
    sl_mod._persist(b"{}", "agent")


# ---------------------------------------------------------------------------
# _fallback_display
# ---------------------------------------------------------------------------


def test_fallback_display_minimal_ctx_only(capsys):
    payload = json.dumps({"context_window": {"used_percentage": 42.0}}).encode()
    sl_mod._fallback_display(payload)
    out = capsys.readouterr().out
    assert "ctx:42%" in out


def test_fallback_display_includes_model_when_present(capsys):
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 10.0},
            "model": {"display_name": "claude-opus-4-7"},
        }
    ).encode()
    sl_mod._fallback_display(payload)
    out = capsys.readouterr().out
    assert "claude-opus-4-7" in out
    assert "ctx:10%" in out


def test_fallback_display_includes_five_hour_pct(capsys):
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 5.0},
            "rate_limits": {"five_hour": {"used_percentage": 22.0}},
        }
    ).encode()
    sl_mod._fallback_display(payload)
    out = capsys.readouterr().out
    assert "5h:22%" in out


def test_fallback_display_omits_five_hour_when_pct_missing(capsys):
    payload = json.dumps(
        {"context_window": {"used_percentage": 1.0}, "rate_limits": {"five_hour": {}}}
    ).encode()
    sl_mod._fallback_display(payload)
    out = capsys.readouterr().out
    assert "5h" not in out


def test_fallback_display_garbage_input_silent(capsys):
    sl_mod._fallback_display(b"not json")
    # No raise, no output.
    assert capsys.readouterr().out == ""


def test_fallback_display_handles_missing_context_window(capsys):
    payload = json.dumps({"other": "fields"}).encode()
    sl_mod._fallback_display(payload)
    out = capsys.readouterr().out
    # default ctx is 0% when missing
    assert "ctx:0%" in out


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _stub_stdin(monkeypatch, raw: bytes) -> None:
    buf = io.BytesIO(raw)

    class _S:
        buffer = buf

    monkeypatch.setattr(sl_mod.sys, "stdin", _S())


def test_main_delegates_to_claude_hud_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(sl_mod, "_agent_name", lambda: "agent-hud")

    payload = json.dumps({"context_window": {"used_percentage": 50}}).encode()
    _stub_stdin(monkeypatch, payload)

    calls = {}

    class _Result:
        returncode = 7

    def fake_run(argv, input=None):
        calls["argv"] = argv
        calls["input"] = input
        return _Result()

    monkeypatch.setattr(sl_mod.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as excinfo:
        sl_mod.main()
    assert excinfo.value.code == 7
    assert calls["argv"] == ["claude-hud"]
    assert calls["input"] == payload

    # Persisted file present.
    assert (tmp_path / "agent-hud.json").exists()


def test_main_falls_back_when_claude_hud_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sl_mod, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(sl_mod, "_agent_name", lambda: "agent-fb")

    payload = json.dumps(
        {
            "context_window": {"used_percentage": 88},
            "model": {"display_name": "M"},
        }
    ).encode()
    _stub_stdin(monkeypatch, payload)

    def raise_fnf(argv, input=None):
        raise FileNotFoundError("no claude-hud")

    monkeypatch.setattr(sl_mod.subprocess, "run", raise_fnf)

    # Should NOT raise SystemExit — falls through to _fallback_display.
    sl_mod.main()
    out = capsys.readouterr().out
    assert "ctx:88%" in out
    assert "M" in out
    assert (tmp_path / "agent-fb.json").exists()
