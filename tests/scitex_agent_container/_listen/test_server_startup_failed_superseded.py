"""P4 read-side supersession: has liveness since refuted a STARTUP_FAILED?

``GET /agents/<name>/status`` relabels (never deletes/renames) a marker as
``startup_failed_superseded`` when ``heartbeat.json``'s MTIME — the field
documented as PROCESS ALIVE, never the ``ts`` payload field — postdates
the marker's ``failed_at`` by at least one staleness window
(``HEARTBEAT_STALE_S``). Mirrors the fixture shape of
``test_server_startup_failed.py``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._startup_failed import MARKER_FILENAME
from scitex_agent_container._lifecycle._verdict_state import HEARTBEAT_STALE_S
from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-startup-failed-superseded"


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    """Redirect $HOME + the SAC dir envs (mirrors test_server_startup_failed.py)."""
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
    env_save_restore.reload_after_restore(ss)
    yield tmp_path


@pytest.fixture
def client(isolated_env):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_spec(home: Path, name: str) -> Path:
    import yaml

    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": explicit_spec(
            {
                "runtime": "tui",
                "host": "${HOSTNAME}",
                "workdir": "/tmp",
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "claude-sonnet-4-5"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            }
        ),
    }
    spec_path = spec_dir / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    return spec_path


def _state_dir(name: str) -> Path:
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    return ss.state_dir_for(name)


def _iso(seconds_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_marker(runtime_dir: Path, *, failed_seconds_ago: float) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "started_at": _iso(failed_seconds_ago),
        "failed_at": _iso(failed_seconds_ago),
        "phase": "container_creation",
        "kind": "apptainer_mount_failed",
        "exit_code": 255,
        "runtime_dir": str(runtime_dir.resolve()),
        "stdout_tail": "",
        "stderr_tail": "FATAL: mount source /work/x doesn't exist",
        "remediation_hint": "",
    }
    (runtime_dir / MARKER_FILENAME).write_text(json.dumps(payload))


def _write_heartbeat(
    runtime_dir: Path, *, ts_seconds_ago: float, mtime_seconds_ago: float
) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    hb = runtime_dir / "heartbeat.json"
    hb.write_text(json.dumps({"pid": 0, "ts": time.time() - ts_seconds_ago}))
    target = time.time() - mtime_seconds_ago
    os.utime(hb, (target, target))


# ---------------------------------------------------------------------------
# A fresh beat postdating the failure supersedes
# ---------------------------------------------------------------------------


def test_status_supersedes_when_a_fresh_beat_postdates_the_failure(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — failed 19 days ago; beat is fresh (just now).
    _write_spec(isolated_env / "home", "fresh-beat")
    sd = _state_dir("fresh-beat")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    _write_heartbeat(sd, ts_seconds_ago=0, mtime_seconds_ago=0)
    # Act
    response = client.get("/agents/fresh-beat/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed_superseded"


def test_superseded_status_still_carries_the_full_marker(
    client, auth_headers, isolated_env
) -> None:
    # Arrange
    _write_spec(isolated_env / "home", "fresh-beat-marker")
    sd = _state_dir("fresh-beat-marker")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    _write_heartbeat(sd, ts_seconds_ago=0, mtime_seconds_ago=0)
    # Act
    response = client.get("/agents/fresh-beat-marker/status", headers=auth_headers)
    # Assert
    assert response.json()["startup_failed"]["kind"] == "apptainer_mount_failed"


def test_superseded_status_names_the_refuting_signal(
    client, auth_headers, isolated_env
) -> None:
    # Arrange
    _write_spec(isolated_env / "home", "fresh-beat-refuter")
    sd = _state_dir("fresh-beat-refuter")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    _write_heartbeat(sd, ts_seconds_ago=0, mtime_seconds_ago=0)
    # Act
    response = client.get("/agents/fresh-beat-refuter/status", headers=auth_headers)
    # Assert
    assert response.json()["startup_failed_superseded_by"] != ""


# ---------------------------------------------------------------------------
# A stale beat does not supersede
# ---------------------------------------------------------------------------


def test_status_does_not_supersede_on_a_stale_beat(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — beat is older than HEARTBEAT_STALE_S.
    _write_spec(isolated_env / "home", "stale-beat")
    sd = _state_dir("stale-beat")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    stale = HEARTBEAT_STALE_S + 3000
    _write_heartbeat(sd, ts_seconds_ago=stale, mtime_seconds_ago=stale)
    # Act
    response = client.get("/agents/stale-beat/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"


# ---------------------------------------------------------------------------
# No heartbeat file at all does not supersede
# ---------------------------------------------------------------------------


def test_status_does_not_supersede_when_there_is_no_heartbeat_file(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — the measured scitex-agentic-journal shape: the one
    # unambiguously TRUE marker in the fleet has no heartbeat.json at all.
    _write_spec(isolated_env / "home", "no-heartbeat")
    sd = _state_dir("no-heartbeat")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    # Act
    response = client.get("/agents/no-heartbeat/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"


# ---------------------------------------------------------------------------
# Supersession reads heartbeat MTIME, never the `ts` payload field
# ---------------------------------------------------------------------------


def test_supersession_uses_mtime_when_ts_is_stale_but_mtime_is_fresh(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — real measured scitex-fixture divergence: `ts` is 70182s
    # stale (would NOT supersede if read), but `mtime` is fresh.
    _write_spec(isolated_env / "home", "mtime-not-ts-a")
    sd = _state_dir("mtime-not-ts-a")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    _write_heartbeat(sd, ts_seconds_ago=70182, mtime_seconds_ago=0)
    # Act
    response = client.get("/agents/mtime-not-ts-a/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed_superseded"


def test_supersession_uses_mtime_when_ts_is_fresh_but_mtime_is_stale(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — inverse: `ts` is fresh (would WRONGLY supersede if read),
    # but `mtime` is a full day stale.
    _write_spec(isolated_env / "home", "mtime-not-ts-b")
    sd = _state_dir("mtime-not-ts-b")
    _write_marker(sd, failed_seconds_ago=19 * 86400)
    _write_heartbeat(sd, ts_seconds_ago=0, mtime_seconds_ago=86400)
    # Act
    response = client.get("/agents/mtime-not-ts-b/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"


# ---------------------------------------------------------------------------
# A beat within one staleness window of the failure does not supersede
# ---------------------------------------------------------------------------


def test_a_beat_within_one_staleness_window_of_the_failure_does_not_supersede(
    client, auth_headers, isolated_env
) -> None:
    # Arrange — the failure itself is only 30s old; its own boot attempt's
    # beat must not be read as refuting it.
    _write_spec(isolated_env / "home", "same-boot-beat")
    sd = _state_dir("same-boot-beat")
    _write_marker(sd, failed_seconds_ago=30)
    _write_heartbeat(sd, ts_seconds_ago=0, mtime_seconds_ago=0)
    # Act
    response = client.get("/agents/same-boot-beat/status", headers=auth_headers)
    # Assert
    assert response.json()["status"] == "startup_failed"
