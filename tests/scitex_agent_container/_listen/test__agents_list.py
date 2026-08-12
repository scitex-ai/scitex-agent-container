"""``GET /agents`` must say WHERE it looked, so an empty list is readable.

INCIDENT 2026-08-09. This route returned ``{"agents": []}`` with HTTP 200
while TWELVE agents were demonstrably alive on the host and exchanging
a2a messages — the registry had briefly lost its registrations. Two
independent sessions read that empty list as "there are no agents", and
one of them escalated fleet-wide data loss off the back of it.

"I cannot see the registry" and "there are no agents" are different
facts, and the route rendered them identically. Row counts alone cannot
separate them — both are zero. So the payload now carries WHICH store
was consulted and what each of the three sources contributed, which is
what lets a caller judge; and a total of zero says outright that it is
not proof no agent is alive.

``agents`` keeps its shape. The additions are a sibling key, so existing
consumers are untouched — asserted here by
:func:`test_agents_key_is_unchanged_for_consumers`.

No mocks (PA-306): a real Starlette route driven through a real
TestClient against a real (empty, tmp) state.db. AAA markers, one
assertion per test.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def empty_store(tmp_path: Path):
    """An isolated, EMPTY registry — the shape the incident produced.

    BOTH the state.db AND the agents dir must be redirected. The route
    has three sources, and isolating only the database still lets the
    self-peer scan walk the real host's spec directory — which on this
    machine holds 109 specs, so the "empty" case is never reached and
    the tests silently assert nothing.
    """
    db = tmp_path / "state.db"
    agents_dir = tmp_path / "agents"
    registry_dir = tmp_path / "registry"
    agents_dir.mkdir()
    registry_dir.mkdir()
    env = {
        "SCITEX_AGENT_CONTAINER_STATE_DB": str(db),
        "SCITEX_AGENT_CONTAINER_AGENTS_DIR": str(agents_dir),
        "SCITEX_AGENT_CONTAINER_YAML_DIRS": str(agents_dir),
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR": str(registry_dir),
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    # CWD matters as much as the env here. The self-peer scan walks a
    # PROJECT-LOCAL `.scitex/agent-container/agents/` relative to the
    # working directory, and this repo ships a `self/spec.yaml` there —
    # so running from the checkout yields a self-peer row no environment
    # variable suppresses, and the "zero rows" case is never reached.
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)

    # RELOAD ORDER IS LOAD-BEARING. Each of these modules binds its path
    # from the environment at IMPORT time, and under pytest they are
    # already imported before this fixture runs — so setting the env
    # alone changes nothing. `_agents_list` must be reloaded LAST because
    # it holds `from .._state.registry import Registry`: reloading
    # `registry` alone leaves the old class, bound to the old directory,
    # still referenced by the route.
    import scitex_agent_container._listen._agents_list as agents_list_mod
    import scitex_agent_container._state.registry as registry_mod
    import scitex_agent_container._state.state_db as state_db_mod

    importlib.reload(state_db_mod)
    importlib.reload(registry_mod)
    importlib.reload(agents_list_mod)
    state_db_mod.init_schema(db)
    try:
        yield db
    finally:
        os.chdir(saved_cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(state_db_mod)
        importlib.reload(registry_mod)
        importlib.reload(agents_list_mod)


@pytest.fixture
def client(empty_store: Path):
    # Imported AFTER empty_store has reloaded the module, so the route
    # bound into the app is the one reading the isolated directories.
    from scitex_agent_container._listen._agents_list import list_agents

    app = Starlette(routes=[Route("/agents", list_agents)])
    return TestClient(app)


def _body(client) -> dict:
    return json.loads(client.get("/agents").content)


def test_empty_result_names_the_store_it_consulted(client, empty_store):
    # Arrange: an empty registry — indistinguishable from a blind one
    # without this field.
    expected = str(empty_store)
    # Act
    body = _body(client)
    # Assert
    assert body["sources"]["store"] == expected


def test_empty_result_reports_per_source_counts(client):
    # Arrange: three sources feed this route; a caller needs to know which
    # of them contributed nothing.
    expected_keys = {"registry_rows", "self_peer_rows", "comms_node_rows"}
    # Act
    body = _body(client)
    # Assert
    assert expected_keys <= set(body["sources"])


def test_zero_rows_warns_it_is_not_proof_that_no_agent_is_alive(client):
    # Arrange: the exact misreading that cost two sessions today.
    # Act
    body = _body(client)
    # Assert
    assert "NOT read this as proof" in body["sources"]["note"]


def test_zero_rows_points_at_an_independent_check(client):
    # Arrange: a caller told only "this might be wrong" is stuck; it needs
    # a check that does NOT go through the registry.
    # Act
    body = _body(client)
    # Assert
    assert "tmux ls" in body["sources"]["note"]


def test_agents_key_is_unchanged_for_consumers(client):
    # Arrange: `sources` is additive. Every existing consumer reads
    # `agents` and must keep working untouched.
    # Act
    body = _body(client)
    # Assert
    assert body["agents"] == []
