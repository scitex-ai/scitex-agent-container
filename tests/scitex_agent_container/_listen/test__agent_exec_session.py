"""Real host request and subprocess boundaries preserve explicit continuity."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container._listen._agent_exec import agents_start
from scitex_agent_container._listen.auth import BearerAuthMiddleware


def isolated_host():
    # The actual administrative ACL path needs no Store; foreground skips the
    # unrelated OAuth boot-settle and post-ack probes. Only a fake SAC runs.
    app = Starlette(routes=[Route("/agents", agents_start, methods=["POST"])])
    app.add_middleware(BearerAuthMiddleware, token="synthetic-host")
    return TestClient(app)


@pytest.mark.parametrize(
    "mode,expected",
    (
        ("continue", ["--session", "continue"]),
        ("fresh", ["--session", "fresh"]),
        ("resume", ["--session", "resume"]),
        ("new-session", ["--session", "fresh"]),
    ),
)
def test_host_session_policy_reaches_the_canonical_cli(subprocess_shim, mode, expected):
    # Arrange
    subprocess_shim.install("sac", stdout="synthetic foreground exit", exit=0)
    body = {"name": "owned", "foreground": True, "assume_yes": True, "session": mode}
    # Act
    with isolated_host() as client:
        client.post(
            "/agents", json=body, headers={"Authorization": "Bearer synthetic-host"}
        )
    # Assert
    assert subprocess_shim.argv_for("sac") == [
        "agents",
        "start",
        "--foreground",
        "--yes",
        *expected,
        "owned",
    ]


def test_host_default_preserves_the_spec_session(subprocess_shim):
    # Arrange
    subprocess_shim.install("sac", stdout="synthetic foreground exit", exit=0)
    # Act
    with isolated_host() as client:
        client.post(
            "/agents",
            json={"name": "owned", "foreground": True},
            headers={"Authorization": "Bearer synthetic-host"},
        )
    # Assert
    assert subprocess_shim.argv_for("sac") == [
        "agents",
        "start",
        "--foreground",
        "owned",
    ]


@pytest.mark.parametrize("mode", ("", "typo", False, {}, []))
def test_invalid_host_session_is_refused_before_launch(subprocess_shim, mode):
    # Arrange
    subprocess_shim.install("sac", stdout="must not execute", exit=0)
    # Act
    with isolated_host() as client:
        result = client.post(
            "/agents",
            json={"name": "owned", "session": mode},
            headers={"Authorization": "Bearer synthetic-host"},
        )
    # Assert
    assert (
        result.status_code,
        result.json()["kind"],
        subprocess_shim.argv_for("sac"),
    ) == (400, "spec_invalid", None)


def test_session_override_does_not_bypass_host_bearer(subprocess_shim):
    # Arrange
    subprocess_shim.install("sac", stdout="must not execute", exit=0)
    # Act
    with isolated_host() as client:
        result = client.post(
            "/agents",
            json={"name": "owned", "session": "continue"},
            headers={"Authorization": "Bearer wrong-synthetic-host"},
        )
    # Assert
    assert (result.status_code, subprocess_shim.argv_for("sac")) == (403, None)
