"""Listen-side ``agents_start`` handler shells the canonical CLI argv.

Regression test for the singular-vs-plural bug exposed by the
SAC-from-SAC live test (operator-mandated 2026-06-01): the handler in
:mod:`_listen._agent_exec` used to shell ``["sac", "agent", "start",
name]`` (singular ``agent``), but the F-CS13 rename removed the
singular group — the host's ``sac`` binary now responds with
``Error: No such command 'agent'.`` and exits non-zero. That single
character broke every brokered spawn end-to-end.

The fix is one token (``agent`` → ``agents``). This test pins the argv
shape so the regression cannot return silently: a future refactor that
flips the form back to singular will fail here, not at 3am when an
in-SIF agent tries to spawn a child and the host listen returns 502.

NO MOCKS — uses the :func:`subprocess_shim` helper from the package
conftest. A real fake ``sac`` binary is dropped on ``$PATH``; the
handler's real ``shutil.which("sac")`` resolves to the shim; the shim
records its argv; the test reads the argv back. This is the same
no-mocks pattern :mod:`tests/scitex_agent_container/cli_pkg/lifecycle`
already uses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db

_TOKEN = "test-token-agent-exec"


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors test__acl.py shape)."""
    db = tmp_path / "state.db"
    saved_env_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default_db = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
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


def test_agents_start_shells_plural_agents_form(
    isolated_listen_env, subprocess_shim
) -> None:
    """The handler MUST shell ``["sac", "agents", "start", <name>]``.

    The singular ``"agent"`` form was removed in F-CS13; using it here
    makes the host's CLI exit non-zero with "No such command 'agent'",
    which manifests as a 502 from every brokered SAC-from-SAC spawn.
    """
    # Arrange — drop a fake ``sac`` on PATH that records its argv.
    subprocess_shim.install("sac", stdout="ok", exit=0)
    app = create_app(token=_TOKEN)
    # Act — admin spawn (no caller) so the gate trivially allows and we
    # reach the subprocess shell-out. Body is the minimum the handler
    # accepts.
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    argv = subprocess_shim.argv_for("sac")
    # Assert — the canonical PLURAL form. A regression to singular
    # (``"agent"``) would fail here loudly.
    assert argv == ["agents", "start", "broker-child"], argv


def test_agents_start_does_not_use_singular_agent_form(
    isolated_listen_env, subprocess_shim
) -> None:
    """Explicit negative — the buggy singular form must NOT recur."""
    # Arrange
    subprocess_shim.install("sac", stdout="ok", exit=0)
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    argv = subprocess_shim.argv_for("sac")
    # Assert — single-token regression guard.
    assert argv is not None and argv[0] != "agent", argv
