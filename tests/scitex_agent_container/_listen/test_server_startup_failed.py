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
    stay isolated from operator's real .scitex tree.

    Reloads ``_session_state`` BOTH on entry (so the module's
    module-level ``DEFAULT_STATE_ROOT`` re-reads our redirected
    ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR``) AND on teardown (so the
    NEXT test in the same worker doesn't inherit our tmp_path as the
    default state root). The module reads the env at import time, so
    a one-shot reload before-the-test leaves the post-env-restore path
    cached and breaks ``_runners/test_claude_session.py``'s assertions
    on the default home-rooted path.
    """
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
    yield tmp_path
    # Pytest finalizes inner-first (LIFO): this fixture's teardown runs
    # BEFORE ``env_save_restore`` restores the env. So we have to pop
    # the env keys ourselves before reloading — otherwise the reload
    # re-binds ``DEFAULT_STATE_ROOT`` to our tmp_path again. After we
    # reload, ``env_save_restore`` will set the env back to its
    # original (pre-test) values, but the module is already cached
    # with the right defaults.
    os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    os.environ.pop("HOME", None)
    importlib.reload(ss)


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
            "runtime": "tui",
            "host": "local",
            "workdir": "/tmp",
            "apptainer": {"image": "/x.sif", "binds": []},
            "claude": {"model": "claude-sonnet-4-5"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
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


def test_delete_410_body_top_level_status_is_startup_failed(
    client, auth_headers, isolated_env
):
    # Arrange — per clew review (#287), the lifecycle tag is at the
    # top level under ``status`` (mirrors the STATUS body's ``status``
    # field) so a renderer can branch without walking into ``details``.
    _write_marker_for("delete-status-tag")
    # Act
    response = client.delete("/agents/delete-status-tag", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"


def test_delete_410_top_level_kind_is_failure_classification(
    client, auth_headers, isolated_env
):
    # Arrange — top-level ``kind`` carries the FAILURE CLASSIFICATION
    # lifted from the marker (e.g. ``apptainer_mount_failed``) so a
    # clew-launcher error renderer can switch on it without walking
    # into ``details``. The lifecycle tag (``startup_failed``) lives
    # under ``status``.
    _write_marker_for("delete-kind-tag")
    # Act
    response = client.delete("/agents/delete-kind-tag", headers=auth_headers)
    # Assert
    assert response.json()["kind"] == "apptainer_mount_failed"


def test_delete_410_body_includes_details_with_failure_kind(
    client, auth_headers, isolated_env
):
    # Arrange — full marker remains echoed under ``details`` so a
    # caller can hash for dedupe or surface the stderr_tail.
    _write_marker_for("delete-details")
    # Act
    response = client.delete("/agents/delete-details", headers=auth_headers)
    details = response.json()["details"]
    # Assert
    assert details["kind"] == "apptainer_mount_failed"


def test_delete_410_body_lifts_phase_to_top_level(client, auth_headers, isolated_env):
    # Arrange — ``phase`` (e.g. ``container_creation``) is one of the
    # summary fields clew lifts from the marker so a one-line error
    # render has the lifecycle phase without a second key-walk.
    _write_marker_for("delete-phase-lift")
    # Act
    response = client.delete("/agents/delete-phase-lift", headers=auth_headers)
    # Assert
    assert response.json()["phase"] == "container_creation"


def test_delete_410_body_lifts_runtime_dir_to_top_level(
    client, auth_headers, isolated_env
):
    # Arrange — ``runtime_dir`` is the host-absolute path to the
    # per-instance state dir. Lifting it to the top level means a
    # human ``cat`` of the marker / peer ``stderr.log`` requires no
    # path reconstruction.
    _write_marker_for("delete-runtime-dir")
    # Act
    response = client.delete("/agents/delete-runtime-dir", headers=auth_headers)
    # Assert
    assert response.json()["runtime_dir"] != ""


def test_delete_410_body_carries_see_also_pointing_at_marker_file(
    client, auth_headers, isolated_env
):
    # Arrange — ``see_also`` is the convenience suffix-join of
    # ``runtime_dir`` + ``STARTUP_FAILED`` so a human / sysadmin gets
    # a copy-paste-able ``cat`` target without computing it.
    _write_marker_for("delete-see-also")
    # Act
    response = client.delete("/agents/delete-see-also", headers=auth_headers)
    body = response.json()
    # Assert
    assert body["see_also"].endswith("/STARTUP_FAILED")


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
