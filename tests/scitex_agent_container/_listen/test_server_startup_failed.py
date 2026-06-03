"""Wired-end-to-end tests for the PR-1 STARTUP_FAILED surface.

Covers the HTTP wire shape that the clew launcher (and future SAC-from-SAC
clients) consumes:

  * ``DELETE /agents/<name>`` returns **410 Gone** with the structured
    failure body when the runtime dir carries a ``STARTUP_FAILED`` marker
    — distinguishing "stillborn" (existed, has been removed) from the
    "never existed" 404 case.
  * ``GET /agents/<name>/status`` echoes the same marker under
    ``startup_failed`` so callers don't need a second round trip.

Uses the real Starlette ``TestClient`` against ``create_app`` + the real
``state_dir_for`` resolver — no mocks, no fakes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._startup_failed import write_marker
from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-startup-failed"


# ---------------------------------------------------------------------------
# Fixtures — mirror the shape used by tests/.../test_server.py
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    """Redirect $HOME + the SAC dir envs so registry / runtime writes
    land in tmp_path. Mirror of test_server.py's fixture so the tests
    stay isolated from operator's real .scitex tree."""
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    yaml_dir = home / ".scitex" / "agent-container" / "agents"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    return tmp_path


@pytest.fixture
def client(isolated_env):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_spec(home: Path, name: str) -> Path:
    """Minimal v3 spec.yaml that ``load_config`` accepts."""
    import yaml

    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "claude": {"model": "claude-sonnet-4-5"},
            "workdir": "/tmp",
        },
    }
    spec_path = spec_dir / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    return spec_path


def _write_marker_for(name: str) -> Path:
    """Write a STARTUP_FAILED marker into ``state_dir_for(name)``."""
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    runtime_dir = ss.state_dir_for(name)
    return write_marker(
        runtime_dir,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="mount source /work/x doesn't exist",
    )


# ---------------------------------------------------------------------------
# DELETE — 410 Gone when stillborn marker present
# ---------------------------------------------------------------------------


def test_delete_returns_410_when_stillborn_marker_present(
    client, auth_headers, isolated_env
):
    # Arrange — marker on disk, no pid file → stillborn.
    _write_marker_for("delete-stillborn")
    # Act
    response = client.delete("/agents/delete-stillborn", headers=auth_headers)
    # Assert
    assert response.status_code == 410


def test_delete_410_body_carries_kind_startup_failed(
    client, auth_headers, isolated_env
):
    # Arrange
    _write_marker_for("delete-kind-tag")
    # Act
    response = client.delete("/agents/delete-kind-tag", headers=auth_headers)
    # Assert
    assert response.json()["kind"] == "startup_failed"


def test_delete_410_body_includes_details_with_failure_kind(
    client, auth_headers, isolated_env
):
    # Arrange
    _write_marker_for("delete-details")
    # Act
    response = client.delete("/agents/delete-details", headers=auth_headers)
    details = response.json()["details"]
    # Assert
    assert details["kind"] == "apptainer_mount_failed"


def test_delete_returns_404_when_no_marker_and_no_pid(
    client, auth_headers, isolated_env
):
    # Arrange — agent has never existed; no marker, no pid.
    # Act
    response = client.delete("/agents/never-existed", headers=auth_headers)
    # Assert
    assert response.status_code == 404


def test_delete_404_distinct_kind_from_410(client, auth_headers, isolated_env):
    # Arrange — never-existed must NOT have the startup_failed kind.
    # Act
    response = client.delete("/agents/genuinely-absent", headers=auth_headers)
    body = response.json()
    # Assert
    assert body.get("kind") != "startup_failed"


# ---------------------------------------------------------------------------
# STATUS — startup_failed echo
# ---------------------------------------------------------------------------


def test_status_status_field_says_startup_failed_when_marker_present(
    client, auth_headers, isolated_env
):
    # Arrange — spec + marker on disk (no live session).
    _write_spec(Path(os.environ["HOME"]), "status-stillborn")
    _write_marker_for("status-stillborn")
    # Act
    response = client.get("/agents/status-stillborn/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"


def test_status_echoes_marker_under_startup_failed_key(
    client, auth_headers, isolated_env
):
    # Arrange
    _write_spec(Path(os.environ["HOME"]), "status-echo")
    _write_marker_for("status-echo")
    # Act
    response = client.get("/agents/status-echo/status", headers=auth_headers)
    # Assert
    assert "startup_failed" in response.json()


def test_status_marker_carries_failure_kind(client, auth_headers, isolated_env):
    # Arrange
    _write_spec(Path(os.environ["HOME"]), "status-kind")
    _write_marker_for("status-kind")
    # Act
    response = client.get("/agents/status-kind/status", headers=auth_headers)
    # Assert
    assert response.json()["startup_failed"]["kind"] == "apptainer_mount_failed"


def test_status_does_not_set_status_field_when_no_marker(
    client, auth_headers, isolated_env
):
    # Arrange — spec only, no marker.
    _write_spec(Path(os.environ["HOME"]), "status-healthy")
    # Act
    response = client.get("/agents/status-healthy/status", headers=auth_headers)
    # Assert — body must not advertise startup_failed when no marker.
    assert "startup_failed" not in response.json()


# ---------------------------------------------------------------------------
# DELETE preserves 200 OK path when a real pid exists
# ---------------------------------------------------------------------------


def test_delete_returns_200_when_pid_present(client, auth_headers, isolated_env):
    # Arrange — a real subprocess to SIGTERM.
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        import importlib

        import scitex_agent_container._runners._session_state as ss

        importlib.reload(ss)
        sd = ss.state_dir_for("delete-live")
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "pid").write_text(str(proc.pid))
        # Act
        response = client.delete("/agents/delete-live", headers=auth_headers)
        # Assert
        assert response.status_code == 200
    finally:
        # stx-allow: fallback (reason: best-effort process cleanup;
        # if the test path already killed it, kill() raises ProcessLookup)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (
            OSError,
            subprocess.TimeoutExpired,
        ):  # stx-allow: fallback (reason: see inline comment)
            pass
