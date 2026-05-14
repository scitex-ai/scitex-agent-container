"""Tests for the sac-statusline command (statusline.py).

No-mocks pattern (PA-306):
- ``$SAC_STATUSLINE_STATE_DIR`` env override redirects the persist dir
  (no module-attribute swap of ``_STATE_DIR``).
- ``main(stdin=, runner=)`` accepts injection seams for the stdin
  bytes-stream and the subprocess runner; tests pass real ``io.BytesIO``
  and hand-rolled callables.
- ``_persist`` OSError path tested by writing to a real read-only
  directory (chmod 0o555).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import scitex_agent_container.statusline as sl_mod
from scitex_agent_container.statusline import _persist, read_statusline_json


@pytest.fixture
def state_dir(tmp_path: Path, env_save_restore) -> Path:
    """Redirect SAC_STATUSLINE_STATE_DIR to tmp_path via env override."""
    env_save_restore.set("SAC_STATUSLINE_STATE_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# read_statusline_json
# ---------------------------------------------------------------------------


def test_read_statusline_json_returns_none_for_missing_file(state_dir):
    # Arrange
    name = "no-such-agent"
    # Act
    result = read_statusline_json(name)
    # Assert
    assert result is None


def test_read_statusline_json_returns_none_for_corrupt_file(state_dir):
    # Arrange
    (state_dir / "bad-agent.json").write_text("not json")
    # Act
    result = read_statusline_json("bad-agent")
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# _persist
# ---------------------------------------------------------------------------


def test_persist_round_trips_via_read_statusline_json(state_dir):
    # Arrange
    payload = {
        "context_window": {"used_percentage": 42.5},
        "model": {"display_name": "claude-sonnet-4-6"},
    }
    raw = json.dumps(payload).encode()
    # Act
    _persist(raw, "test-agent")
    # Assert
    result = read_statusline_json("test-agent")
    assert result is not None and result["context_window"]["used_percentage"] == 42.5


def test_persist_atomic_no_tmp_file_left_behind(state_dir):
    # Arrange
    _persist(json.dumps({"v": 1}).encode(), "agent-x")
    # Act
    _persist(json.dumps({"v": 2}).encode(), "agent-x")
    # Assert
    assert not (state_dir / "agent-x.json.tmp").exists()


def test_persist_overwrites_previous_payload(state_dir):
    # Arrange
    _persist(json.dumps({"v": 1}).encode(), "agent-x")
    # Act
    _persist(json.dumps({"v": 2}).encode(), "agent-x")
    # Assert
    assert json.loads((state_dir / "agent-x.json").read_text())["v"] == 2


def test_persist_swallows_oserror_when_target_dir_unwritable(
    tmp_path, env_save_restore
):
    # Arrange — state dir exists but is read-only so write_bytes fails.
    readonly = tmp_path / "readonly_state"
    readonly.mkdir()
    readonly.chmod(0o555)
    env_save_restore.set("SAC_STATUSLINE_STATE_DIR", str(readonly))
    # Act
    try:
        result = _persist(b"{}", "agent")
        # Assert — production must catch OSError and return None.
        assert result is None
    finally:
        readonly.chmod(0o755)


# ---------------------------------------------------------------------------
# _agent_name — pure env-driven, no mocks.
# ---------------------------------------------------------------------------


def test_agent_name_prefers_sac_agent_env_var(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_AGENT", "from-sac-env")
    env_save_restore.set("CLAUDE_AGENT_ID", "ignored")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    # Act
    name = sl_mod._agent_name()
    # Assert
    assert name == "from-sac-env"


def test_agent_name_falls_back_to_claude_agent_id(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_AGENT")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.set("CLAUDE_AGENT_ID", "claude-aid")
    # Act
    name = sl_mod._agent_name()
    # Assert
    assert name == "claude-aid"


def test_agent_name_returns_unknown_with_no_env_set(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_AGENT")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_AGENT")
    env_save_restore.delete("CLAUDE_AGENT_ID")
    # Act
    name = sl_mod._agent_name()
    # Assert
    assert name == "unknown"


# ---------------------------------------------------------------------------
# _fallback_display
# ---------------------------------------------------------------------------


def test_fallback_display_renders_context_percent(capsys):
    # Arrange
    payload = json.dumps({"context_window": {"used_percentage": 42.0}}).encode()
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert "ctx:42%" in capsys.readouterr().out


def test_fallback_display_includes_model_display_name(capsys):
    # Arrange
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 10.0},
            "model": {"display_name": "claude-opus-4-7"},
        }
    ).encode()
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert "claude-opus-4-7" in capsys.readouterr().out


def test_fallback_display_includes_five_hour_used_pct(capsys):
    # Arrange
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 5.0},
            "rate_limits": {"five_hour": {"used_percentage": 22.0}},
        }
    ).encode()
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert "5h:22%" in capsys.readouterr().out


def test_fallback_display_omits_five_hour_when_pct_absent(capsys):
    # Arrange
    payload = json.dumps(
        {"context_window": {"used_percentage": 1.0}, "rate_limits": {"five_hour": {}}}
    ).encode()
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert "5h" not in capsys.readouterr().out


def test_fallback_display_silent_on_garbage_input(capsys):
    # Arrange
    payload = b"not json"
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert capsys.readouterr().out == ""


def test_fallback_display_defaults_ctx_to_zero_when_missing(capsys):
    # Arrange
    payload = json.dumps({"other": "fields"}).encode()
    # Act
    sl_mod._fallback_display(payload)
    # Assert
    assert "ctx:0%" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class _Stdin:
    """Real bytes-stream stand-in for sys.stdin (not a Mock)."""

    def __init__(self, raw: bytes):
        self.buffer = io.BytesIO(raw)


def _run_main_capturing_systemexit(stdin_bytes, runner_fn):
    """Run sl_mod.main and return the SystemExit, or None if not raised."""
    try:
        sl_mod.main(stdin=_Stdin(stdin_bytes), runner=runner_fn)
        return None
    except SystemExit as exc:
        return exc


def test_main_propagates_claude_hud_exit_code(state_dir, env_save_restore):
    # Arrange
    env_save_restore.set("CLAUDE_AGENT_ID", "agent-hud")
    payload = json.dumps({"context_window": {"used_percentage": 50}}).encode()

    class _Result:
        returncode = 7

    # Act
    exc = _run_main_capturing_systemexit(payload, lambda argv, input=None: _Result())
    # Assert
    assert exc is not None and exc.code == 7


def test_main_forwards_payload_to_claude_hud_stdin(state_dir, env_save_restore):
    # Arrange
    env_save_restore.set("CLAUDE_AGENT_ID", "agent-hud2")
    payload = json.dumps({"context_window": {"used_percentage": 50}}).encode()
    captured: dict = {}

    class _Result:
        returncode = 0

    def runner(argv, input=None):
        captured["input"] = input
        return _Result()

    # Act
    _run_main_capturing_systemexit(payload, runner)
    # Assert
    assert captured["input"] == payload


def test_main_persists_payload_before_delegating(state_dir, env_save_restore):
    # Arrange
    env_save_restore.set("CLAUDE_AGENT_ID", "agent-persist")
    payload = json.dumps({"context_window": {"used_percentage": 50}}).encode()

    class _Result:
        returncode = 0

    # Act
    _run_main_capturing_systemexit(payload, lambda argv, input=None: _Result())
    # Assert
    assert (state_dir / "agent-persist.json").exists()


def test_main_falls_back_to_local_display_when_claude_hud_missing(
    state_dir, env_save_restore, capsys
):
    # Arrange
    env_save_restore.set("CLAUDE_AGENT_ID", "agent-fb")
    payload = json.dumps(
        {
            "context_window": {"used_percentage": 88},
            "model": {"display_name": "M"},
        }
    ).encode()

    def runner(argv, input=None):
        raise FileNotFoundError("no claude-hud")

    # Act
    sl_mod.main(stdin=_Stdin(payload), runner=runner)
    # Assert
    assert "ctx:88%" in capsys.readouterr().out


def test_main_fallback_path_persists_payload(state_dir, env_save_restore):
    # Arrange
    env_save_restore.set("CLAUDE_AGENT_ID", "agent-fb-persist")
    payload = json.dumps({"context_window": {"used_percentage": 88}}).encode()

    def runner(argv, input=None):
        raise FileNotFoundError("no claude-hud")

    # Act
    sl_mod.main(stdin=_Stdin(payload), runner=runner)
    # Assert
    assert (state_dir / "agent-fb-persist.json").exists()
