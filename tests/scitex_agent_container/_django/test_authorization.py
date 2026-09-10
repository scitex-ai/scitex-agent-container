"""Scope + operator-authorization unit tests (no HTTP, no Django client)."""

from __future__ import annotations

import json

from .conftest import CROSS_AGENT, LOCAL_NAME, OWN_AGENT, REMOTE_NAME


def test_local_row_is_own_scope():
    from scitex_agent_container._django._authorization import is_own_scope

    assert is_own_scope(OWN_AGENT) is True  # no host field


def test_this_node_row_is_own_scope():
    from scitex_agent_container._django._authorization import is_own_scope

    assert is_own_scope({"name": "x", "host": LOCAL_NAME}) is True
    assert is_own_scope({"name": "x", "host": "localhost"}) is True


def test_other_node_row_is_cross_scope():
    from scitex_agent_container._django._authorization import is_own_scope

    assert is_own_scope({"name": "x", "host": REMOTE_NAME}) is False
    assert is_own_scope(CROSS_AGENT) is False


def test_ordinary_identity_sees_only_own_scope():
    from scitex_agent_container._django._authorization import scope_rows

    from .conftest import OWN_AGENT_DEAD

    rows = scope_rows([OWN_AGENT, OWN_AGENT_DEAD, CROSS_AGENT], identity="alice")
    names = {r["name"] for r in rows}
    assert names == {"alpha", "beta"}
    assert all(r["scope"] == "own" for r in rows)


def test_crosshost_operator_sees_fleet_tagged():
    import os

    from scitex_agent_container._django._authorization import scope_rows

    from .conftest import OWN_AGENT_DEAD

    os.environ["SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS"] = "op1"
    try:
        rows = scope_rows([OWN_AGENT, OWN_AGENT_DEAD, CROSS_AGENT], identity="op1")
        names = {r["name"] for r in rows}
        assert names == {"alpha", "beta", "gamma"}
        gamma = next(r for r in rows if r["name"] == "gamma")
        assert gamma["scope"] == "cross-host"
    finally:
        os.environ.pop("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", None)


def test_can_control_own_requires_operator_list():
    import os

    from scitex_agent_container._django._authorization import can_control

    os.environ["SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS"] = "op1"
    try:
        assert can_control("op1", cross_host=False) is True
        assert can_control("stranger", cross_host=False) is False
        assert can_control("", cross_host=False) is False
    finally:
        os.environ.pop("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", None)


def test_can_control_crosshost_requires_stricter_list(audit_log):
    import os

    from scitex_agent_container._django._authorization import can_control

    os.environ["SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS"] = "localop"
    os.environ["SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS"] = "crossop"
    try:
        # A local-scope operator is NOT a cross-host operator.
        assert can_control("localop", cross_host=True) is False
        # The cross-host operator is allowed and audited.
        assert can_control("crossop", cross_host=True, agent="gamma") is True
        lines = audit_log.read_text().strip().splitlines()
        assert lines
        rec = json.loads(lines[-1])
        assert rec["event"] == "crosshost_control_granted"
        assert rec["identity"] == "crossop"
        assert rec["agent"] == "gamma"
    finally:
        os.environ.pop("SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS", None)
        os.environ.pop("SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS", None)


def test_no_identity_is_never_an_operator():
    from scitex_agent_container._django._authorization import can_control

    assert can_control("", cross_host=False) is False
    assert can_control("", cross_host=True) is False


def test_resolve_identity_prefers_django_user():
    from scitex_agent_container._django._authorization import resolve_identity

    class _Req:
        class user:
            username = "django-user"

    assert resolve_identity(_Req()) == "django-user"


def test_resolve_identity_falls_back_to_declared():
    import os

    from scitex_agent_container._django._authorization import resolve_identity

    os.environ["SCITEX_AGENT_CONTAINER_GUI_IDENTITY"] = "declared"
    try:
        class _Req:
            user = None

        assert resolve_identity(_Req()) == "declared"
    finally:
        os.environ.pop("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", None)
