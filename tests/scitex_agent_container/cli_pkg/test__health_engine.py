"""``engine_payload`` must report the engine the process was LAUNCHED on.

Every test below either covers the original defect (nothing reported the
resolved engine) or one of the six found in adversarial review of the
first cut. The review's own reproductions are the fixtures.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._health_engine import (
    REASON_BLIND_VANTAGE,
    REASON_ENGINES_DISAGREE,
    REASON_ENVIRONS_UNREADABLE,
    REASON_NO_RUNNING_PROCESS,
    VANTAGE_CONTAINER,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_UNKNOWN,
    EngineScan,
    _read_running_engine,
    engine_payload,
)

SPEC_SELECTING_QWEN = {
    "engines": {
        "qwen38-27b": {"default": True},
        "claude": {},
    }
}


def _reader(engine, reason=None, scan=None):
    """Stand in for the /proc scan."""

    def read(_name):
        return engine, scan or EngineScan(pids_scanned=1, pids_matched=1), reason

    return read


class _AgentConfigLike:
    """The shape `load_config` returns: a MERGED engines map + engine_key."""

    def __init__(self, engines, engine_key=""):
        self.engines = engines
        self.engine_key = engine_key


def _proc(tmp_path, entries):
    """Fake /proc: {pid: {VAR: value}} -> NUL-separated environ files."""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, variables in entries.items():
        directory = root / str(pid)
        directory.mkdir()
        block = "\0".join(f"{k}={v}" for k, v in variables.items())
        (directory / "environ").write_bytes(block.encode("utf-8"))
    (root / "notapid").mkdir()
    return root


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


def test_a_spec_declaring_no_engine_never_inherits_one_from_the_fleet_library():
    # Arrange
    merged_namespace = {"only-fleet-engine": object()}
    config = _AgentConfigLike(merged_namespace, engine_key="")

    # Act
    payload = engine_payload("scitex-cards", config, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["declared"] is None


def test_a_spec_declaring_no_engine_is_unknown_rather_than_a_mismatch():
    # Arrange
    config = _AgentConfigLike({"only-fleet-engine": object()}, engine_key="")

    # Act
    payload = engine_payload("scitex-cards", config, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["verdict"] == VERDICT_UNKNOWN


def test_an_explicit_engine_pin_in_a_raw_spec_beats_the_default_flag():
    # Arrange
    spec = {"engine": "claude", "engines": {"qwen38-27b": {"default": True}, "claude": {}}}

    # Act
    payload = engine_payload("business", spec, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["declared"] == "claude"


def test_the_declared_engine_is_read_from_a_real_agent_config_object():
    # Arrange
    config = _AgentConfigLike({}, engine_key="qwen38-27b")

    # Act
    payload = engine_payload("business", config, launch_engine_reader=_reader("claude"))

    # Assert
    assert payload["declared"] == "qwen38-27b"


def test_a_reader_that_raises_degrades_to_unknown_instead_of_crashing_health():
    # Arrange
    def exploding(_name):
        raise OSError("proc went away")

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


def test_the_real_reader_takes_the_engine_from_the_running_process(tmp_path):
    # Arrange
    root = _proc(tmp_path, {961961: {"CLAUDE_AGENT_ID": "business", "SAC_ENGINE": "claude"}})

    # Act
    engine, _scan, _reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert engine == "claude"


def test_a_process_belonging_to_another_agent_is_not_read(tmp_path):
    # Arrange
    root = _proc(tmp_path, {700: {"CLAUDE_AGENT_ID": "figrecipe", "SAC_ENGINE": "qwen38-27b"}})

    # Act
    engine, _scan, _reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert engine is None


def test_no_running_process_is_reported_as_such_when_the_scan_was_complete(tmp_path):
    # Arrange
    root = _proc(tmp_path, {})

    # Act
    _engine, _scan, reason = _read_running_engine("never-started", proc_root=root)

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
    _engine, _scan, reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert reason.startswith(REASON_ENGINES_DISAGREE)


def test_unreadable_process_environments_are_reported_not_read_as_absence(tmp_path):
    # Arrange
    root = _proc(tmp_path, {})
    denied = root / "4242"
    denied.mkdir()
    (denied / "environ").write_bytes(b"CLAUDE_AGENT_ID=business\0SAC_ENGINE=claude")
    (denied / "environ").chmod(0o000)

    # Act
    _engine, _scan, reason = _read_running_engine("business", proc_root=root)

    # Assert
    assert reason.startswith(REASON_ENVIRONS_UNREADABLE)


def test_a_container_vantage_cannot_claim_the_agent_is_absent():
    # Arrange
    scan = EngineScan(pids_scanned=18, pids_matched=0, vantage=VANTAGE_CONTAINER)

    # Act
    payload = engine_payload(
        "business",
        SPEC_SELECTING_QWEN,
        launch_engine_reader=_reader(None, reason=REASON_BLIND_VANTAGE, scan=scan),
    )

    # Assert
    assert payload["reason"] == REASON_BLIND_VANTAGE


def test_a_partial_scan_is_never_reported_as_complete():
    # Arrange
    scan = EngineScan(pids_scanned=283, pids_matched=1, pids_unreadable=235)

    # Act
    complete = scan.to_dict()["complete"]

    # Assert
    assert complete is False


def test_the_scan_census_travels_with_a_successful_answer_too():
    # Arrange
    scan = EngineScan(pids_scanned=283, pids_matched=13, pids_unreadable=235)

    # Act
    payload = engine_payload(
        "business", SPEC_SELECTING_QWEN, launch_engine_reader=_reader("claude", scan=scan)
    )

    # Assert
    assert payload["scan"]["pids_unreadable"] == 235


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
