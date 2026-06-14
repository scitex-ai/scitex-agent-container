"""Tests for the session-movement fields on ``agent_status`` payload.

Operator mandate (lead a2a 1781e82a, 2026-06-14): the per-agent
``sac agents status <name> --json`` envelope must carry
``session_jsonl_bytes`` / ``session_jsonl_last_write`` / ``heartbeat_at``
as top-level keys so the kick-cycle reads MOVEMENT directly.

Real registry / real config / real ``ClaudeSessionRuntime`` (no
container running → ``is_running=False``); no mocks. AAA markers on
separate lines; one assertion per test (STX-TQ007).
"""

from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_runtime(tmp_path: Path, env_save_restore):
    """Redirect the runtime root + reload ``_session_state`` so the
    helper's ``state_dir_for(name)`` lookup lands inside ``tmp_path``.
    """
    root = tmp_path / "runtime"
    root.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    import scitex_agent_container._runners._session_state as _ss

    importlib.reload(_ss)
    return root


@pytest.fixture
def isolated_registry(tmp_path: Path):
    """Real ``Registry`` rooted in ``tmp_path/reg``."""
    from scitex_agent_container._state.registry import Registry

    return Registry(registry_dir=tmp_path / "reg")


def _write_valid_spec(parent: Path, name: str) -> Path:
    agent_dir = parent / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
    )
    return spec


def test_status_payload_includes_session_jsonl_bytes_key(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert "session_jsonl_bytes" in result


def test_status_payload_includes_session_jsonl_last_write_key(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert "session_jsonl_last_write" in result


def test_status_payload_includes_heartbeat_at_key(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert "heartbeat_at" in result


def test_status_payload_session_jsonl_bytes_zero_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange — no state dir was materialised under ``isolated_runtime``.
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert result["session_jsonl_bytes"] == 0


def test_status_payload_session_jsonl_last_write_empty_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert result["session_jsonl_last_write"] == ""


def test_status_payload_heartbeat_at_empty_when_no_state_dir(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "alpha")
    isolated_registry.add("alpha", str(spec), "cld-alpha")
    # Act
    result = agent_status("alpha", registry=isolated_registry)
    # Assert
    assert result["heartbeat_at"] == ""


def test_status_payload_session_jsonl_bytes_matches_real_file_size(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange — state dir + real session.jsonl + heartbeat.json on disk.
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "live")
    isolated_registry.add("live", str(spec), "cld-live")
    state_dir = isolated_runtime / "live"
    state_dir.mkdir()
    payload = b'{"type":"assistant","text":"alive"}\n'
    (state_dir / "session.jsonl").write_bytes(payload)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "pid": 1, "state": "idle"}),
        encoding="utf-8",
    )
    # Act
    result = agent_status("live", registry=isolated_registry)
    # Assert
    assert result["session_jsonl_bytes"] == len(payload)


def test_status_payload_heartbeat_at_non_empty_when_heartbeat_present(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "live2")
    isolated_registry.add("live2", str(spec), "cld-live2")
    state_dir = isolated_runtime / "live2"
    state_dir.mkdir()
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": 1_750_000_000.0, "pid": 1, "state": "working"}),
        encoding="utf-8",
    )
    # Act
    result = agent_status("live2", registry=isolated_registry)
    # Assert
    assert result["heartbeat_at"] != ""


def test_status_payload_session_jsonl_last_write_iso_format_when_file_present(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange
    import re

    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "live3")
    isolated_registry.add("live3", str(spec), "cld-live3")
    state_dir = isolated_runtime / "live3"
    state_dir.mkdir()
    jsonl = state_dir / "session.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")
    pinned = 1_750_000_000.0
    os.utime(jsonl, (pinned, pinned))
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")
    # Act
    result = agent_status("live3", registry=isolated_registry)
    # Assert
    assert iso_re.match(result["session_jsonl_last_write"]) is not None


def test_status_payload_existing_keys_remain_after_movement_enrichment(
    tmp_path: Path, isolated_runtime: Path, isolated_registry
):
    # Arrange — additive contract: legacy fields like name / status /
    # hooks_configured must still be present alongside the new keys.
    from scitex_agent_container._lifecycle._status import agent_status

    spec = _write_valid_spec(tmp_path, "compat")
    isolated_registry.add("compat", str(spec), "cld-compat")
    # Act
    result = agent_status("compat", registry=isolated_registry)
    # Assert
    assert set(("name", "status", "hooks_configured", "listen")) <= set(result)
