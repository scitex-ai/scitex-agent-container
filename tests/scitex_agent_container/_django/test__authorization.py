"""Tests for ``_django/_authorization`` (scoping + operator gate + audit).

Pure-function coverage — no HTTP, no mocks. Env is set through the shared
function-scoped ``env_save_restore`` fixture (auto-restored), never monkeypatch.
Each test has AAA markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scitex_agent_container._django._authorization import (
    can_control,
    is_own_scope,
    local_hostname,
    resolve_identity,
    scope_rows,
)

CROSS_ENV = "SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS"
OPS_ENV = "SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS"
IDENT_ENV = "SCITEX_AGENT_CONTAINER_GUI_IDENTITY"
AUDIT_ENV = "SCITEX_AGENT_CONTAINER_GUI_AUDIT_LOG"

LOCAL = local_hostname()
REMOTE = "remote-node-" + (LOCAL[:8] or "x")


def _rows():
    return [
        {"name": "alpha", "turn_url": f"http://{LOCAL}:19000/v1/turn"},
        {"name": "beta", "turn_url": f"http://{LOCAL}:19001/v1/turn"},
        {"name": "gamma", "turn_url": f"http://{REMOTE}:19002/v1/turn"},
    ]


def test_local_row_is_own_scope():
    # Arrange
    row = {"name": "alpha", "turn_url": f"http://{LOCAL}:19000/v1/turn"}
    # Act
    own = is_own_scope(row)
    # Assert
    assert own is True


def test_row_without_turn_url_is_own_scope():
    # Arrange
    row = {"name": "alpha"}
    # Act
    own = is_own_scope(row)
    # Assert
    assert own is True


def test_other_node_row_is_cross_scope():
    # Arrange
    row = {"name": "gamma", "turn_url": f"http://{REMOTE}:19002/v1/turn"}
    # Act
    own = is_own_scope(row)
    # Assert
    assert own is False


def test_this_node_host_is_own_scope():
    # Arrange
    row = {"name": "x", "host": LOCAL}
    # Act
    own = is_own_scope(row)
    # Assert
    assert own is True


def test_ordinary_identity_sees_only_own_scope():
    # Arrange
    rows = _rows()
    # Act
    visible = scope_rows(rows, identity="alice")
    # Assert
    assert {r["name"] for r in visible} == {"alpha", "beta"}


def test_crosshost_operator_sees_fleet():
    # Arrange
    os.environ[CROSS_ENV] = "op1"
    rows = _rows()
    try:
        visible = scope_rows(rows, identity="op1")
    finally:
        del os.environ[CROSS_ENV]
    # Act
    names = {r["name"] for r in visible}
    # Assert
    assert names == {"alpha", "beta", "gamma"}


def test_crosshost_row_is_tagged():
    # Arrange
    os.environ[CROSS_ENV] = "op1"
    rows = _rows()
    try:
        visible = scope_rows(rows, identity="op1")
    finally:
        del os.environ[CROSS_ENV]
    # Act
    gamma = next(r for r in visible if r["name"] == "gamma")
    # Assert
    assert gamma["scope"] == "cross-host"


def test_own_operator_controls_own_agent():
    # Arrange
    os.environ[OPS_ENV] = "op1"
    try:
        # Act
        allowed = can_control("op1", cross_host=False)
    finally:
        del os.environ[OPS_ENV]
    # Assert
    assert allowed is True


def test_stranger_cannot_control_own_agent():
    # Arrange
    os.environ[OPS_ENV] = "op1"
    try:
        allowed = can_control("stranger", cross_host=False)
    finally:
        del os.environ[OPS_ENV]
    # Act
    denied = not allowed
    # Assert
    assert denied


def test_no_identity_is_never_an_operator():
    # Arrange
    os.environ[OPS_ENV] = "op1"
    os.environ[CROSS_ENV] = "op1"
    try:
        # Act
        own_ok = can_control("", cross_host=False)
        cross_ok = can_control("", cross_host=True)
    finally:
        del os.environ[OPS_ENV]
        del os.environ[CROSS_ENV]
    # Assert
    assert own_ok is False and cross_ok is False


def test_local_operator_is_not_a_crosshost_operator():
    # Arrange
    os.environ[OPS_ENV] = "localop"
    os.environ[CROSS_ENV] = "crossop"
    try:
        # Act
        allowed = can_control("localop", cross_host=True, agent="gamma")
    finally:
        del os.environ[OPS_ENV]
        del os.environ[CROSS_ENV]
    # Assert
    assert allowed is False


def test_crosshost_operator_is_allowed():
    # Arrange
    os.environ[CROSS_ENV] = "crossop"
    os.environ[AUDIT_ENV] = "/tmp/gui-audit-never-written.log"
    try:
        # Act
        allowed = can_control("crossop", cross_host=True, agent="gamma")
    finally:
        del os.environ[CROSS_ENV]
        del os.environ[AUDIT_ENV]
    # Assert
    assert allowed is True


def test_crosshost_grant_is_audited():
    # Arrange
    audit = "/tmp/gui-audit-crosshost-grant.log"
    os.environ[CROSS_ENV] = "crossop"
    os.environ[AUDIT_ENV] = audit
    try:
        can_control("crossop", cross_host=True, agent="gamma")
        last = json.loads(Path(audit).read_text().strip().splitlines()[-1])
    finally:
        del os.environ[CROSS_ENV]
        del os.environ[AUDIT_ENV]
        os.remove(audit)
    # Act
    is_grant = last.get("event") == "crosshost_control_granted"
    # Assert
    assert is_grant and last.get("agent") == "gamma"


def test_resolve_identity_prefers_django_user():
    # Arrange
    class _Req:
        class user:
            username = "django-user"

    # Act
    identity = resolve_identity(_Req())
    # Assert
    assert identity == "django-user"


def test_resolve_identity_falls_back_to_declared():
    # Arrange
    os.environ[IDENT_ENV] = "declared"
    try:
        class _Req:
            user = None

        # Act
        identity = resolve_identity(_Req())
    finally:
        del os.environ[IDENT_ENV]
    # Assert
    assert identity == "declared"
