#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``--force`` must survive the in-SIF broker hop (incident 2026-07-12).

``agent_restart`` calls ``agent_start(force=True)`` precisely because a
restart's contract is to REPLACE the process. But when the caller runs
INSIDE a SIF, ``agent_start`` brokers the spawn to the host's ``sac
listen`` BEFORE that ``force`` is ever consulted locally — and the broker
used to have no ``force`` parameter at all, so the flag was silently
dropped at the boundary.

The host then ran a plain, unforced ``sac agents start <name>``, hit the
idempotent "already running -> no-op" branch, printed ``SUCC: <name>
started`` and exited 0. Observed consequence on ``scitex-storage``::

    Agent 'scitex-storage' is already running. No-op. Use --force to restart.
    SUCC: scitex-storage started (...)

    [listen post-ack liveness probe] post_ack_no_apptainer_pid: `sac agents
    start` returned rc=0 but no apptainer_pid file appeared ... within 5.0s.

The restart reported success while NOTHING cycled — same process, same
pid, same stale credentials — and because no new container was launched,
no ``apptainer_pid`` was ever written, which is what tripped the
post-ack probe.

These tests pin the wire contract end to end: the spawn client emits the
field, the broker forwards it, and the host handler turns it into a real
``--force`` on the inner argv.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._spawn_client import request_spawn
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db

_TOKEN = "test-token-force-propagation"


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


class _FakeResponse:
    """Minimal ``urllib.response``-shaped object (no network, no mocks)."""

    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    """Captures the POST body the spawn client actually puts on the wire."""

    def __init__(self):
        self.bodies: list[dict] = []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse({"name": "victim", "returncode": 0})


class TestSpawnClientPutsForceOnTheWire:
    """The field has to leave the container before anything can honour it."""

    def test_force_true_is_emitted_in_the_post_body(self):
        # Arrange
        opener = _RecordingOpener()
        # Act
        request_spawn(
            "victim",
            base_url="http://listen.invalid",
            bearer="tok",
            opener=opener,
            force=True,
        )
        # Assert: THE regression guard — before the fix this key did not
        # exist, so the host could not tell a restart from a plain start.
        assert opener.bodies[0].get("force") is True

    def test_force_is_absent_by_default_for_back_compat(self):
        # Arrange
        opener = _RecordingOpener()
        # Act
        request_spawn(
            "victim",
            base_url="http://listen.invalid",
            bearer="tok",
            opener=opener,
        )
        # Assert: an ordinary brokered start must keep its idempotent
        # behaviour, and a pre-fix host must keep ignoring the field.
        assert "force" not in opener.bodies[0]


class TestBrokerForwardsForce:
    """The chokepoint agent_start calls must accept and forward force."""

    def test_maybe_broker_in_sif_spawn_accepts_force(self):
        # Arrange
        from scitex_agent_container._lifecycle._in_sif_broker import (
            maybe_broker_in_sif_spawn,
        )

        # Act
        params = inspect.signature(maybe_broker_in_sif_spawn).parameters
        # Assert
        assert "force" in params

    def test_broker_start_to_host_accepts_force(self):
        # Arrange
        from scitex_agent_container._lifecycle._in_sif_broker import (
            broker_start_to_host,
        )

        # Act
        params = inspect.signature(broker_start_to_host).parameters
        # Assert
        assert "force" in params

    def test_agent_start_passes_force_into_the_broker_call(self):
        # Arrange: read the real production source. This is the exact line
        # whose absence caused the incident.
        from scitex_agent_container._lifecycle import _start

        source = inspect.getsource(_start.agent_start)
        broker_call = source.split("maybe_broker_in_sif_spawn(", 1)[1].split("):", 1)[0]
        # Act
        forwards_force = "force=force" in broker_call
        # Assert
        assert forwards_force is True, (
            "agent_start must forward its own `force` into the in-SIF "
            "broker; the broker fires BEFORE the local force branch, so "
            "omitting it downgrades a RESTART into an unforced start that "
            "no-ops over the live agent and still reports SUCC (2026-07-12)"
        )


class TestHostHandlerHonoursForce:
    """The host must turn the wire field into a REAL ``--force`` argv.

    These drive the actual handler over HTTP and read back the argv a
    real fake ``sac`` binary on ``$PATH`` recorded — no mocks, and no
    source-text assertions. An earlier draft of this class asserted that
    ``'inner_argv.append("--force")'`` appeared in the module source;
    mutating the guard to ``if False:`` left that string in place, so the
    test stayed GREEN over dead code. A test whose evidence cannot
    disagree with it is not a test.
    """

    def test_force_true_appends_force_to_the_inner_argv(
        self, isolated_listen_env, env_save_restore, subprocess_shim
    ):
        # Arrange: the post-ack liveness probe is stood down (the shim
        # writes no apptainer_pid); it has its own dedicated suite.
        env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
        subprocess_shim.install("sac", stdout="ok", exit=0)
        app = create_app(token=_TOKEN)
        # Act
        with TestClient(app) as client:
            client.post(
                "/agents",
                json={"name": "broker-child", "force": True},
                headers={"authorization": f"Bearer {_TOKEN}"},
            )
        argv = subprocess_shim.argv_for("sac")
        # Assert: THE regression guard — without this flag the host start
        # no-ops over the live agent and still reports SUCC + rc=0.
        assert "--force" in (argv or []), argv

    def test_force_absent_leaves_the_inner_argv_unforced(
        self, isolated_listen_env, env_save_restore, subprocess_shim
    ):
        # Arrange
        env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
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
        # Assert: an ordinary brokered start keeps its idempotent
        # behaviour — the fix must not force every spawn.
        assert "--force" not in (argv or []), argv

    def test_non_boolean_force_is_rejected_with_400(
        self, isolated_listen_env, env_save_restore, subprocess_shim
    ):
        # Arrange: reject rather than coerce, matching the foreground /
        # one_shot / assume_yes precedent.
        env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
        subprocess_shim.install("sac", stdout="ok", exit=0)
        app = create_app(token=_TOKEN)
        # Act
        with TestClient(app) as client:
            response = client.post(
                "/agents",
                json={"name": "broker-child", "force": "yes"},
                headers={"authorization": f"Bearer {_TOKEN}"},
            )
        # Assert
        assert response.status_code == 400, response.text
