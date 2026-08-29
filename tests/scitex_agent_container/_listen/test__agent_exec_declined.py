"""P1 wire-level tests: a brokered decline must not mint STARTUP_FAILED,
and a genuine brokered failure still must.

Real ``TestClient`` + a real fake ``sac`` shim on ``$PATH`` (mirrors
``test__agent_exec_subprocess.py``) — no mocks. The shim prints the
exact refusal banner + the shared decline sentinel to stderr and exits
non-zero, exactly like a real ``sac agents start`` refusal does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._startup_failed import read_marker
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._runners._session_state import state_dir_for
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db

_TOKEN = "test-token-agent-exec-declined"


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors test__agent_exec_subprocess.py)."""
    db = tmp_path / "state.db"
    saved_env_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default_db = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        state_db.DEFAULT_DB_PATH = saved_default_db
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_env_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env_db
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _install_declining_sac_shim(bin_dir: Path) -> None:
    """A shim that behaves exactly like a real `--yes`-less refusal."""
    from scitex_agent_container._lifecycle._start_decline import DECLINE_SENTINEL

    script = bin_dir / "sac"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'sys.stderr.write("refusing to start x without --yes/-y — the plan '
        "above shows exactly what will mount and run; re-run with --yes to "
        'launch.\\n")\n'
        f'sys.stderr.write({DECLINE_SENTINEL!r} + "\\n")\n'
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def _install_crashing_sac_shim(bin_dir: Path) -> None:
    """A shim simulating a genuine apptainer FATAL — no sentinel anywhere."""
    script = bin_dir / "sac"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        'sys.stderr.write("FATAL: container creation failed: mount source '
        "/work/x doesn't exist\\n\")\n"
        "sys.exit(255)\n"
    )
    script.chmod(0o755)


class _Posted(NamedTuple):
    status_code: int
    body: dict
    marker: dict | None


def _post_agents(
    env_save_restore, tmp_path: Path, *, name: str, declining: bool
) -> _Posted:
    bin_dir = tmp_path / f"sac_bin_{'decline' if declining else 'crash'}"
    bin_dir.mkdir()
    if declining:
        _install_declining_sac_shim(bin_dir)
    else:
        _install_crashing_sac_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    app = create_app(token=_TOKEN)
    with TestClient(app) as client:
        resp = client.post(
            "/agents",
            json={"name": name},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    return _Posted(
        status_code=resp.status_code,
        body=resp.json(),
        marker=read_marker(state_dir_for(name)),
    )


# ---------------------------------------------------------------------------
# A declined brokered start writes no marker
# ---------------------------------------------------------------------------


def test_a_declined_brokered_start_returns_502(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="declined-child", declining=True
    )
    # Assert
    assert result.status_code == 502


def test_a_declined_brokered_start_body_marks_declined_true(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="declined-body", declining=True
    )
    # Assert
    assert result.body["declined"] is True


def test_a_declined_brokered_start_writes_no_marker(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="declined-no-marker", declining=True
    )
    # Assert
    assert result.marker is None


# ---------------------------------------------------------------------------
# A genuine launch failure still writes a marker
# ---------------------------------------------------------------------------


def test_a_genuine_launch_failure_returns_502(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="crash-child", declining=False
    )
    # Assert
    assert result.status_code == 502


def test_a_genuine_launch_failure_body_marks_declined_false(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="crash-body", declining=False
    )
    # Assert
    assert result.body["declined"] is False


def test_a_genuine_launch_failure_writes_a_marker(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="crash-marker", declining=False
    )
    # Assert
    assert result.marker is not None


def test_a_genuine_launch_failure_marker_classifies_apptainer_mount_failed(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    # (fixtures above build the isolated state dirs + shim)
    # Act
    result = _post_agents(
        env_save_restore, tmp_path, name="crash-kind", declining=False
    )
    # Assert
    assert result.marker["kind"] == "apptainer_mount_failed"
