"""``engine_payload`` must report the engine the process was LAUNCHED on.

The defect these cover: nothing reported the resolved engine, so an agent
could run 27 hours on a backend its own spec forbade and every surface
still said it was fine.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._health_engine import (
    REASON_ENGINES_DISAGREE,
    REASON_NO_RUNNING_PROCESS,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_UNKNOWN,
    _read_running_engine,
    engine_payload,
)

SPEC_SELECTING_QWEN = {
    "engines": {
        "qwen38-27b": {"default": True},
        "claude": {},
    }
}


def _reader(engine, observed_at="2026-09-06T00:00:00+00:00", reason=None):
    """Stand in for the launch-env snapshot read."""

    def read(_name):
        return engine, observed_at, reason

    return read


def test_an_agent_launched_on_a_different_engine_than_the_spec_selects_is_a_mismatch():
    # Arrange
    reader = _reader("claude")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=reader)

    # Assert
    assert payload["verdict"] == VERDICT_MISMATCH


def test_an_agent_launched_on_the_engine_its_spec_selects_is_not_flagged():
    # Arrange
    reader = _reader("qwen38-27b")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=reader)

    # Assert
    assert payload["verdict"] == VERDICT_MATCH


def test_an_agent_declared_on_claude_and_running_claude_is_not_flagged():
    # Arrange
    spec = {"engines": {"claude": {"default": True}}}

    # Act
    payload = engine_payload("cards", spec, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["verdict"] == VERDICT_MATCH


def test_a_missing_launch_snapshot_is_unknown_rather_than_a_match():
    # Arrange
    reader = _reader(None, observed_at=None, reason="no-launch-env-snapshot")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=reader)

    # Assert
    assert payload["verdict"] == VERDICT_UNKNOWN


def test_a_reader_that_raises_degrades_to_unknown_instead_of_crashing_health():
    # Arrange
    def exploding(_name):
        raise OSError("snapshot volume went away")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=exploding)

    # Assert
    assert payload["verdict"] == VERDICT_UNKNOWN


def test_the_running_engine_is_taken_from_the_launch_environment_not_the_spec():
    # Arrange
    reader = _reader("claude")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=reader)

    # Assert
    assert payload["running"] == "claude"


def test_the_declared_engine_is_taken_from_the_spec():
    # Arrange
    reader = _reader("claude")

    # Act
    payload = engine_payload("business", SPEC_SELECTING_QWEN, launch_engine_reader=reader)

    # Assert
    assert payload["declared"] == "qwen38-27b"


def _proc(tmp_path, entries):
    """Build a fake /proc: {pid: {VAR: value}} -> null-separated environ files."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, variables in entries.items():
        directory = root / str(pid)
        directory.mkdir()
        block = "\0".join(f"{k}={v}" for k, v in variables.items())
        (directory / "environ").write_bytes(block.encode("utf-8"))
    (root / "notapid").mkdir()
    return root


def test_the_real_reader_takes_the_engine_from_the_running_process(tmp_path):
    # Arrange
    root = _proc(tmp_path, {961961: {"CLAUDE_AGENT_ID": "business", "SAC_ENGINE": "claude"}})

    # Act
    engine, _observed_at, _reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert engine == "claude"


def test_a_process_belonging_to_another_agent_is_not_read():
    # Arrange
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        root = _proc(
            Path(directory),
            {700: {"CLAUDE_AGENT_ID": "figrecipe", "SAC_ENGINE": "qwen38-27b"}},
        )

        # Act
        engine, _observed_at, _reason = _read_running_engine("business", proc_root=root)

        # Assert
        assert engine is None


def test_no_running_process_is_reported_as_such_rather_than_guessed(tmp_path):
    # Arrange
    root = _proc(tmp_path, {})

    # Act
    _engine, _observed_at, reason = _read_running_engine("never-started", proc_root=root)

    # Assert
    assert reason == REASON_NO_RUNNING_PROCESS


def test_live_processes_disagreeing_on_the_engine_is_surfaced_not_averaged(tmp_path):
    # Arrange
    root = _proc(
        tmp_path,
        {
            11: {"CLAUDE_AGENT_ID": "business", "SAC_ENGINE": "claude"},
            12: {"CLAUDE_AGENT_ID": "business", "SAC_ENGINE": "qwen38-27b"},
        },
    )

    # Act
    _engine, _observed_at, reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert reason.startswith(REASON_ENGINES_DISAGREE)


def test_the_business_incident_would_now_be_reported_as_a_mismatch(tmp_path):
    # Arrange
    root = _proc(tmp_path, {961961: {"CLAUDE_AGENT_ID": "business", "SAC_ENGINE": "claude"}})

    # Act
    payload = engine_payload(
        "business",
        SPEC_SELECTING_QWEN,
        launch_engine_reader=lambda n: _read_running_engine(n, proc_root=root),
    )

    # Assert
    assert payload["verdict"] == VERDICT_MISMATCH


class _AgentConfigLike:
    """The shape `load_config` actually returns: engines + engine_key."""

    def __init__(self, engines, engine_key=""):
        self.engines = engines
        self.engine_key = engine_key


def test_the_declared_engine_is_read_from_a_real_agent_config_object():
    # Arrange
    from scitex_agent_container.config._engine_types import parse_engines

    config = _AgentConfigLike(parse_engines(SPEC_SELECTING_QWEN))

    # Act
    payload = engine_payload("business", config, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["declared"] == "qwen38-27b"


def test_an_explicit_engine_pin_on_the_config_wins_over_the_default():
    # Arrange
    from scitex_agent_container.config._engine_types import parse_engines

    config = _AgentConfigLike(parse_engines(SPEC_SELECTING_QWEN), engine_key="claude")

    # Act
    payload = engine_payload("business", config, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["verdict"] == VERDICT_MATCH


def test_unreadable_process_environments_are_reported_not_read_as_absence(tmp_path):
    # Arrange
    from scitex_agent_container.cli_pkg._health_engine import REASON_ENVIRONS_UNREADABLE

    root = _proc(tmp_path, {})
    denied = root / "4242"
    denied.mkdir()
    (denied / "environ").write_bytes(b"CLAUDE_AGENT_ID=business\0SAC_ENGINE=claude")
    (denied / "environ").chmod(0o000)

    # Act
    _engine, _observed_at, reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert reason.startswith(REASON_ENVIRONS_UNREADABLE)
