"""Smoke-layer pytest config.

The `smoke` marker is *also* registered in pyproject.toml so
`pytest -m smoke` works from any directory; this conftest keeps the
layer self-contained for partial checkouts.

It also hosts the shared fixtures for the node-comms smoke tests
(``disk_tmp`` + ``comms_env``), used by both the HTTP and MCP
variants. Plain (non-fixture) helpers live in ``_node_comms.py``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "smoke: fast user-facing happy-path tests (<60s total); "
        "runs on every PR. Opt out with '-m \"not smoke\"'.",
    )


# ---------------------------------------------------------------------------
# Disk-backed tmp dir.
#
# Inside the agent container ``/tmp`` is a 64 MB tmpfs that ``state.db``
# + WAL writes can fill on a busy run, so we prefer the container's
# writable ``/work`` overlay. On a host / CI checkout there is no
# ``/work``; there we fall back to pytest's ``tmp_path_factory`` so the
# suite still runs (and does not error at collection on a missing dir).
# ---------------------------------------------------------------------------


@pytest.fixture
def disk_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A fresh temp dir — ``/work``-backed in-container, else ``tmp_path``.

    Created per-test; removed in ``finally`` so a failure does not
    leave state.db files behind.
    """
    work = Path("/work")
    if os.access(work, os.W_OK):
        base = work / ".pytest-tmp" / "smoke-node-comms"
        base.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(dir=base))
    else:
        path = tmp_path_factory.mktemp("smoke-node-comms")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def comms_env(pg_schema: str, disk_tmp: Path) -> Iterator[dict[str, Any]]:
    """Isolated state.db + HOME + registry + runtime roots + PostgreSQL schema.

    ``pg_schema`` FIRST, and it is not decoration. When this fixture was
    written the comms state lived entirely in the ``state.db`` below, so
    isolating a tmp SQLite file and HOME was the whole job. The ACL tables
    have since moved to the per-host PostgreSQL store, and without this
    dependency the deny path writes its rate-limit rows into the LIVE fleet
    store — measured 2026-08-20, ``alpha``/``beta``/``gamma`` rows in
    production, and a 30-minute cool-down that then made
    ``test_listen_denied_send_persists_two_channel_events_rows`` fail on
    every re-run within the window.

    Ordered before ``disk_tmp`` deliberately: ``pg_schema`` connects while
    HOME is still real, and this fixture goes on to sandbox HOME. libpq
    finds its password file under HOME, so the reverse order strands the
    schema's own CREATE.

    Touches every read-path the comms code may consult (env var,
    module-level constant) so neither code path leaks into the
    developer's real ``~/.scitex/agent-container`` tree.
    """
    db = disk_tmp / "state.db"

    saved_env = {
        "HOME": os.environ.get("HOME"),
        "SCITEX_AGENT_CONTAINER_STATE_DB": os.environ.get(
            "SCITEX_AGENT_CONTAINER_STATE_DB"
        ),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_REGISTRY_DIR"
        ),
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR": os.environ.get(
            "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
        ),
        "SCITEX_AGENT_CONTAINER_YAML_DIRS": os.environ.get(
            "SCITEX_AGENT_CONTAINER_YAML_DIRS"
        ),
    }
    saved_consts = {
        "state_db": state_db.DEFAULT_DB_PATH,
        "registry": _reg.REGISTRY_DIR,
        "session_state": _ss.DEFAULT_STATE_ROOT,
    }

    os.environ["HOME"] = str(disk_tmp)
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SCITEX_AGENT_CONTAINER_REGISTRY_DIR"] = str(disk_tmp / "registry")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(disk_tmp / "runtime")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    state_db.DEFAULT_DB_PATH = db
    _reg.REGISTRY_DIR = disk_tmp / "registry"
    _ss.DEFAULT_STATE_ROOT = disk_tmp / "runtime"
    state_db.init_schema(db)
    try:
        yield {"db": db, "tmp": disk_tmp}
    finally:
        state_db.DEFAULT_DB_PATH = saved_consts["state_db"]
        _reg.REGISTRY_DIR = saved_consts["registry"]
        _ss.DEFAULT_STATE_ROOT = saved_consts["session_state"]
        for key, val in saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
