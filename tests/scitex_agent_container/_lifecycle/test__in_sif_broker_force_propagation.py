#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The in-SIF spawn broker rejects legacy force without lifecycle work."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._lifecycle._spawn_client import (
    SpawnRequestError,
    request_spawn,
)
from scitex_agent_container._lifecycle._in_sif_broker import (
    InSifBrokerError,
    broker_start_to_host,
)
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg

_TOKEN = "test-token-force-propagation"


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors test__acl.py shape)."""
    db = tmp_path / "state.db"
    saved_env_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
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


class TestSpawnClientRefusesForce:
    """Force must be refused before the client performs a POST."""

    def test_force_true_is_refused_before_a_network_request(self):
        # Arrange
        opener = _RecordingOpener()
        refusal = ""
        # Act
        try:
            request_spawn(
                "victim",
                base_url="http://listen.invalid",
                bearer="tok",
                opener=opener,
                force=True,
            )
        except SpawnRequestError as exc:
            refusal = str(exc)
        # Assert
        assert ("force is unsupported" in refusal, opener.bodies) == (True, [])

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


class TestBrokerRefusesForce:
    def test_broker_refuses_force_before_a_network_request(self):
        # Arrange
        opener = _RecordingOpener()
        refusal = ""
        # Act
        try:
            broker_start_to_host(
                "victim",
                base_url="http://listen.invalid",
                bearer="tok",
                opener=opener,
                force=True,
            )
        except InSifBrokerError as exc:
            refusal = str(exc)
        # Assert
        assert ("force is unsupported" in refusal, opener.bodies) == (True, [])


class TestHostHandlerRefusesForce:
    """Real HTTP requests carrying force must not spawn any host command."""

    def test_force_true_is_refused_before_any_host_command(
        self, isolated_listen_env, env_save_restore, subprocess_shim
    ):
        # Arrange: the post-ack liveness probe is stood down (the shim
        # writes no apptainer_pid); it has its own dedicated suite.
        env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
        subprocess_shim.install("sac", stdout="ok", exit=0)
        app = create_app(token=_TOKEN)
        # Act
        with TestClient(app) as client:
            response = client.post(
                "/agents",
                json={"name": "broker-child", "force": True},
                headers={"authorization": f"Bearer {_TOKEN}"},
            )
        argv = subprocess_shim.argv_for("sac")
        # Assert
        assert response.status_code == 400 and not argv

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
