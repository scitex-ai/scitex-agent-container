"""The in-container host_exec client presents the per-agent token.

The client takes an ``opener`` seam (``urllib.request.urlopen``-shaped); a
hand-rolled opener captures the real ``urllib.request.Request`` it was
handed. No mocks. AAA-marked, one assert per test.
"""

from __future__ import annotations

import json
import os

from scitex_agent_container._lifecycle._host_exec_client import request_host_exec
from scitex_agent_container._listen.agent_tokens import AGENT_TOKEN_ENV

# urllib normalises header names with str.capitalize().
_HEADER_KEY = "X-sac-agent-token"


class _Response:
    def read(self) -> bytes:
        return json.dumps({"exit_code": 0}).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_a) -> None:  # noqa: ANN002
        return None


def _capturing_opener():
    def _opener(req, timeout=None):  # noqa: ANN001
        _opener.last_request = req
        return _Response()

    _opener.last_request = None  # type: ignore[attr-defined]
    return _opener


def _send(**kwargs):
    opener = _capturing_opener()
    request_host_exec(
        ["true"],
        base_url="http://127.0.0.1:7878",
        bearer="host-bearer-fixture",
        opener=opener,
        **kwargs,
    )
    return opener.last_request


def test_client_sends_explicit_agent_token_header():
    # Arrange
    token = "agent-token-fixture"
    # Act
    req = _send(agent_token=token, caller="dev-agent")
    # Assert
    assert req.get_header(_HEADER_KEY) == token


def test_client_reads_agent_token_from_injected_env():
    # Arrange — the env var sac delivers via the 0600 env-file.
    prev = os.environ.get(AGENT_TOKEN_ENV)
    os.environ[AGENT_TOKEN_ENV] = "env-agent-token-fixture"
    # Act
    try:
        req = _send(caller="dev-agent")
    finally:
        if prev is None:
            os.environ.pop(AGENT_TOKEN_ENV, None)
        else:
            os.environ[AGENT_TOKEN_ENV] = prev
    # Assert
    assert req.get_header(_HEADER_KEY) == "env-agent-token-fixture"


def test_client_sends_no_agent_token_header_when_none_injected():
    # Arrange — an agent started by an older sac: legacy path, no header.
    token = ""
    # Act
    req = _send(agent_token=token, caller="dev-agent")
    # Assert
    assert req.get_header(_HEADER_KEY) is None
