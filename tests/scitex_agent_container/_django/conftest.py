"""Tests for the browser-facing Agents dashboard (``_django``).

Run from the worktree with the worktree ``src`` on the path::

    DJANGO_SETTINGS_MODULE=scitex_agent_container._django._test_settings \
    PYTHONPATH="$PWD/src" pytest tests/scitex_agent_container/_django

The live listener is NOT touched: ``RemoteFleet.from_environment`` is replaced
with a fake, so the view/service layer is exercised against deterministic rows
while still running the real scope, authorization, and projection code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# Force the in-package Django settings for this suite (independent of the repo's
# own conftest floors, which sandbox a different concern).
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "scitex_agent_container._django._test_settings"
)

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from scitex_agent_container._django import _remote  # noqa: E402
from scitex_agent_container._django._authorization import (  # noqa: E402
    can_control,
    is_own_scope,
    record_audit,
    resolve_identity,
    scope_rows,
)
from scitex_agent_container._django._projection import (  # noqa: E402
    project_detail,
    project_row,
)

# Two nodes: this one (own scope) and a remote one (cross-host). The row's node
# is read from `turn_url`'s hostname (the authoritative signal, per the real
# listener) — the registry is fleet-wide and rows carry no `host` field.
# LOCAL_NAME is derived from the real hostname so the tests are node-independent;
# REMOTE_NAME is guaranteed to differ from it.
import socket as _socket

LOCAL_NAME = _socket.gethostname() or "this-node"
_REMOTE = "remote-node-" + (LOCAL_NAME[:8] if LOCAL_NAME else "x")
REMOTE_NAME = _REMOTE

OWN_AGENT = {
    "name": "alpha",
    "role": "worker",
    "project": "proj-a",
    "pid": 111,
    "a2a_port": 19000,
    "turn_url": f"http://{LOCAL_NAME}:19000/v1/turn",
    "started_at": "2026-09-01T00:00:00Z",
}
OWN_AGENT_DEAD = dict(OWN_AGENT, name="beta", pid=222, a2a_port=19001,
                      turn_url=f"http://{LOCAL_NAME}:19001/v1/turn")
CROSS_AGENT = {
    "name": "gamma",
    "role": "worker",
    "project": "proj-g",
    "pid": 333,
    "a2a_port": 19002,
    "turn_url": f"http://{REMOTE_NAME}:19002/v1/turn",
    "started_at": "2026-09-01T00:00:00Z",
}

# Per-agent status observations (the /agents/<name>/status shape).
STATUS: dict[str, dict[str, Any]] = {
    "alpha": {"name": "alpha", "liveness": {"verdict": "ALIVE"}, "status": "running",
              "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
              "model": "sonnet", "pid": 111, "session_id": "a" * 32,
              "a2a_port": 19000, "turn_url": f"http://{LOCAL_NAME}:19000/v1/turn",
              "inbox_reachable": "true"},
    "beta": {"name": "beta", "liveness": {"verdict": "DEAD"}, "status": "stopped",
             "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
             "model": "haiku", "pid": 222, "session_id": "b" * 32,
             "a2a_port": 19001, "turn_url": f"http://{LOCAL_NAME}:19001/v1/turn",
             "inbox_reachable": "unknown"},
    "gamma": {"name": "gamma", "liveness": {"verdict": "ALIVE"}, "status": "running",
              "runtime": "apptainer", "harness": "anthropic", "engine": "anthropic",
              "model": "sonnet", "pid": 333, "session_id": "c" * 32,
              "a2a_port": 19002, "turn_url": f"http://{REMOTE_NAME}:19002/v1/turn",
              "inbox_reachable": "true"},
}


class FakeFleet:
    """Deterministic stand-in for :class:`RemoteFleet` (no network)."""

    def __init__(self, base_url: str = "http://127.0.0.1:7878") -> None:
        self.base_url = base_url
        self.calls: list[tuple[str, str, Any]] = []

    def list_all(self):
        self.calls.append(("GET", "/agents", None))
        return [dict(r) for r in (OWN_AGENT, OWN_AGENT_DEAD, CROSS_AGENT)]

    def read_statuses(self, names):
        out = {}
        for n in names:
            self.calls.append(("GET", f"/agents/{n}/status", None))
            out[n] = dict(STATUS.get(n, {}))
        return out

    def read_status(self, name):
        self.calls.append(("GET", f"/agents/{name}/status", None))
        return dict(STATUS.get(name, {}))

    def lifecycle(self, name, action):
        self.calls.append(("POST/DELETE", name, action))
        return {"status": "accepted", "message": f"{action} {name}"}


@pytest.fixture
def fake_fleet(monkeypatch):
    fleet = FakeFleet()

    def _from_env(*a, **k):
        return fleet

    monkeypatch.setattr(_remote.RemoteFleet, "from_environment", _from_env)
    return fleet


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_AUDIT_LOG", str(log))
    return log


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", "op1")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", "op1")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "op1")
    return "op1"


@pytest.fixture
def crosshost_operator(monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", "localop")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", "crossop")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "crossop")
    return "crossop"


@pytest.fixture
def client():
    return Client()
