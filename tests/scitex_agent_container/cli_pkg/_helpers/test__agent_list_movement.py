"""Tests for the session-movement fields on ``get_agent_list_data`` rows.

Operator mandate (lead a2a 1781e82a, 2026-06-14): each per-agent
row in the ``sac agents status --json`` fleet view must carry
``session_jsonl_bytes`` / ``session_jsonl_last_write`` / ``heartbeat_at``
as top-level keys so the kick-cycle can read MOVEMENT without
scraping ``heartbeat.json`` out of band.

Real ``tmp_path`` directories, no mocks; AAA markers on separate
lines; one assertion per test (STX-TQ007).
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_runtime(tmp_path: Path, env_save_restore):
    """Redirect the runtime root + reload ``_session_state`` so
    ``DEFAULT_STATE_ROOT`` picks up the redirected value. Real reload,
    no monkeypatch.

    THE RELOAD MUST BE UNDONE. ``env_save_restore`` puts the ENV VAR back, but
    ``DEFAULT_STATE_ROOT`` is a MODULE CONSTANT baked at import — so without the
    second reload it stays pinned at THIS test's tmp dir (which pytest then
    deletes) for the remainder of the xdist worker's session, and every later
    test in that worker reads a dangling root out of the process global.

    ``reload_after_restore`` puts that reload on the correct side of the env
    restore (``env_save_restore`` tears down AFTER this fixture, since we depend
    on it, so reloading in our own ``finally`` would re-derive the tmp path we
    are trying to forget).
    """
    root = tmp_path / "runtime"
    root.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    import scitex_agent_container._runners._session_state as _ss

    importlib.reload(_ss)
    env_save_restore.reload_after_restore(_ss)
    return root


class _FakeRegistry:
    """Hand-rolled stand-in for ``Registry`` — same shape, no MagicMock."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def list_all(self) -> list[dict]:
        return list(self._entries)


def _write_valid_spec(dir_: Path) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
    )
    return spec


def _no_discover() -> list[tuple[str, Path]]:
    return []


def _entry(name: str, spec: Path) -> dict:
    return {
        "name": name,
        "screen": f"cld-{name}",
        "started_at": "2026-06-14T00:00:00Z",
        "config": str(spec),
    }


def _registered_row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


def test_row_includes_session_jsonl_bytes_key_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert "session_jsonl_bytes" in _registered_row(rows, "alpha")


def test_row_includes_session_jsonl_last_write_key_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert "session_jsonl_last_write" in _registered_row(rows, "alpha")


def test_row_includes_heartbeat_at_key_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert "heartbeat_at" in _registered_row(rows, "alpha")


def test_row_session_jsonl_bytes_is_zero_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert _registered_row(rows, "alpha")["session_jsonl_bytes"] == 0


def test_row_session_jsonl_last_write_is_empty_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert _registered_row(rows, "alpha")["session_jsonl_last_write"] == ""


def test_row_heartbeat_at_is_empty_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "alpha")
    registry = _FakeRegistry([_entry("alpha", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert _registered_row(rows, "alpha")["heartbeat_at"] == ""


def test_row_session_jsonl_bytes_matches_file_size_when_state_dir_present(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange — materialise a state dir with a real session.jsonl + heartbeat.
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "live")
    state_dir = isolated_runtime / "live"
    state_dir.mkdir()
    payload = b'{"type":"assistant","text":"ok"}\n'
    (state_dir / "session.jsonl").write_bytes(payload)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    registry = _FakeRegistry([_entry("live", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert _registered_row(rows, "live")["session_jsonl_bytes"] == len(payload)


def test_row_heartbeat_at_is_non_empty_when_state_dir_present(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "live2")
    state_dir = isolated_runtime / "live2"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": 1_750_000_000.0, "pid": 1, "state": "working"}),
        encoding="utf-8",
    )
    registry = _FakeRegistry([_entry("live2", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    # Assert
    assert _registered_row(rows, "live2")["heartbeat_at"] != ""


def test_row_existing_keys_remain_after_movement_enrichment(
    tmp_path: Path, isolated_runtime: Path
):
    # Arrange — existing-key backward-compat: name/status/path must
    # still appear next to the new movement keys (additive contract).
    import scitex_agent_container.cli_pkg._helpers._agent_list as _al

    spec = _write_valid_spec(tmp_path / "compat")
    registry = _FakeRegistry([_entry("compat", spec)])
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = _no_discover  # type: ignore[assignment]
    try:
        # Act
        rows = _al.get_agent_list_data(registry)
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]
    row = _registered_row(rows, "compat")
    # Assert
    assert set(("name", "status", "path", "account")) <= set(row)
