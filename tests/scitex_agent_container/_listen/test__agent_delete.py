"""Unit test mirroring ``src/scitex_agent_container/_listen/_agent_delete.py``.

The ``agent_delete`` handler was extracted from ``server.py`` (which hit
the per-file line cap). The exhaustive wire-shape coverage lives in
``test_server_startup_failed.py`` / ``test_server_lineage_acl.py`` (which
drive it through ``create_app``). This file is the PS-204 §2 mirror: it
asserts the extraction's public surface — the handler is importable from
its new home AND still serves the ``404`` not-found path through the real
app over a real ASGI round-trip.

No mocks (STX-NM002): the real Starlette app via the real ``TestClient``
against a real isolated runtime dir.

TQ: AAA markers (TQ002); 3+-word names; the env fixture is FUNCTION
scoped (TQ004) and ``yield``s (TQ005).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen._agent_delete import agent_delete
from scitex_agent_container._listen.server import create_app

TOKEN = "test-agent-delete-token"


@pytest.fixture
def isolated_runtime(tmp_path: Path):
    """Redirect $HOME + runtime dir into tmp_path so delete touches no real
    state. Function-scoped (TQ004); ``yield``s after reloading the state
    module against the redirected env (TQ005); restores on teardown.
    """
    import scitex_agent_container._runners._session_state as ss

    saved_home = os.environ.get("HOME")
    saved_runtime = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    os.environ["HOME"] = str(home)
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(runtime)
    importlib.reload(ss)
    try:
        yield tmp_path
    finally:
        os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_runtime is not None:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = saved_runtime
        importlib.reload(ss)


def test_agent_delete_is_importable_from_extracted_module() -> None:
    # Arrange — the extraction must keep the handler importable from its
    # new home (the route registration imports it from here).
    handler = agent_delete
    # Act
    is_callable = callable(handler)
    # Assert
    assert is_callable


def test_delete_unknown_agent_returns_404(isolated_runtime) -> None:
    # Arrange — agent never existed: no pid file, no marker.
    app = create_app(token=TOKEN)
    headers = {"authorization": f"Bearer {TOKEN}"}
    # Act
    with TestClient(app) as client:
        response = client.delete("/agents/never-existed-xyz", headers=headers)
    # Assert
    assert response.status_code == 404
