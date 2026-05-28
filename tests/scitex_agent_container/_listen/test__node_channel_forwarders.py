"""PS-204 mirror coverage for ``_listen/_node_channel_forwarders.py``.

The end-to-end transport-selector behaviour (HTTP leg, ssh-shim leg,
ACL pass-through, cross-group grant) lives in
``test_server.py`` alongside the existing WI-4 cross-host suite —
that's the canonical mirror for the entire cross-host stack and the
five new ADR-0015 Stage-2 tests are appended there per the lead's
PS-204 directive.

This file holds the focused per-module probes that don't fit the
end-to-end harness: missing-port surface + missing-peer-token surface,
both as direct calls on ``_forward_to_remote``. Same single-assertion
shape as the rest of the package (PA-307).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from scitex_agent_container._listen._node_channel_forwarders import (
    _forward_to_remote,
)
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg


@pytest.fixture
def isolated_home(tmp_path: Path):
    """Pin HOME / registry / runtime so the forwarder's peer-token
    lookup hits an empty registry under tmp_path."""
    saved_home = os.environ.get("HOME")
    saved_reg = _reg.REGISTRY_DIR
    saved_ss = _ss.DEFAULT_STATE_ROOT
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg
        _ss.DEFAULT_STATE_ROOT = saved_ss
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _read_json_body(resp: JSONResponse) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def test_forward_to_remote_502s_when_target_port_is_missing(isolated_home):
    # Arrange
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-a",
            target_port=None,
            target_name="alice",
        )
    )
    # Assert
    assert resp.status_code == 502


def test_forward_to_remote_502_body_names_missing_a2a_port_field(isolated_home):
    # Arrange
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-a",
            target_port=None,
            target_name="alice",
        )
    )
    # Assert
    assert "missing a2a_port" in _read_json_body(resp).get("error", "")


def test_forward_to_remote_502_when_peer_token_file_is_absent(isolated_home):
    # Arrange — no peer-tokens written under HOME, so the loud-502
    # branch fires before any transport selector runs.
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-z",
            target_port=9999,
            target_name="alice",
        )
    )
    # Assert
    assert resp.status_code == 502
