"""The auth middleware resolves a per-agent token to the agent's name.

Real :class:`BearerAuthMiddleware` on a minimal real Starlette app via
:class:`starlette.testclient.TestClient`; real token records written by
``commit_agent_token`` into a ``tmp_path`` runtime dir (the real env var,
set and restored — no monkeypatch). AAA-marked, one assert per test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container._listen.agent_tokens import (
    AGENT_TOKEN_HEADER,
    commit_agent_token,
    stage_agent_token,
)
from scitex_agent_container._listen.auth import BearerAuthMiddleware
from scitex_agent_container._runtime_paths import RUNTIME_DIR_ENV

HOST_BEARER = "host-bearer-fixture"


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Iterator[Path]:
    prev = os.environ.get(RUNTIME_DIR_ENV)
    os.environ[RUNTIME_DIR_ENV] = str(tmp_path / "runtime")
    try:
        yield tmp_path / "runtime"
    finally:
        if prev is None:
            os.environ.pop(RUNTIME_DIR_ENV, None)
        else:
            os.environ[RUNTIME_DIR_ENV] = prev


def _client() -> TestClient:
    async def whoami(request):
        return JSONResponse(
            {"agent": getattr(request.state, "authenticated_agent", None)}
        )

    app = Starlette(routes=[Route("/v1/whoami", whoami)])
    app.add_middleware(BearerAuthMiddleware, token=HOST_BEARER)
    return TestClient(app)


def _committed_token(tmp_path: Path, name: str) -> str:
    state = tmp_path / "state" / name
    token = stage_agent_token(state)
    commit_agent_token(name, state_dir=state)
    return token


def test_valid_agent_token_sets_authenticated_agent(runtime_dir: Path, tmp_path: Path):
    # Arrange
    token = _committed_token(tmp_path, "dev-agent")
    headers = {"Authorization": f"Bearer {HOST_BEARER}", AGENT_TOKEN_HEADER: token}
    # Act
    got = _client().get("/v1/whoami", headers=headers).json()
    # Assert
    assert got["agent"] == "dev-agent"


def test_unknown_agent_token_leaves_agent_unauthenticated(runtime_dir: Path):
    # Arrange
    headers = {"Authorization": f"Bearer {HOST_BEARER}", AGENT_TOKEN_HEADER: "nope"}
    # Act
    got = _client().get("/v1/whoami", headers=headers).json()
    # Assert
    assert got["agent"] is None


def test_agent_token_without_host_bearer_is_still_rejected(
    runtime_dir: Path, tmp_path: Path
):
    # Arrange — the agent token supplements the host bearer, never replaces it.
    token = _committed_token(tmp_path, "dev-agent")
    # Act
    resp = _client().get("/v1/whoami", headers={AGENT_TOKEN_HEADER: token})
    # Assert
    assert resp.status_code == 401
