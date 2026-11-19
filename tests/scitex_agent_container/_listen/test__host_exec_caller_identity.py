"""``POST /v1/host_exec`` takes its caller from a credential, not the body.

Drives the real handler with the same keyword seams the existing host_exec
tests use (``group_resolver``, ``audit_writer``, ``require_agent_token``) —
hand-rolled callables, no mocks. ``_FakeRequest.state`` carries exactly the
attributes the auth middleware sets. AAA-marked, one assert per test.
"""

from __future__ import annotations

import asyncio
import os
import types
from typing import Any

from scitex_agent_container._listen._host_exec import host_exec
from scitex_agent_container._listen._host_exec_identity import (
    IDENTITY_AGENT_TOKEN,
    IDENTITY_NAME_CLAIMED,
    IDENTITY_UNRECOGNISED_TOKEN,
    REQUIRE_AGENT_TOKEN_ENV,
    require_agent_token_from_env,
)


class _FakeRequest:
    def __init__(
        self,
        body: dict[str, Any],
        *,
        authenticated_agent: str | None = None,
        agent_token_presented: bool = False,
    ) -> None:
        self._body = body
        self.state = types.SimpleNamespace(
            authenticated_agent=authenticated_agent,
            agent_token_presented=agent_token_presented,
        )

    async def json(self) -> dict[str, Any]:
        return self._body


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(req: _FakeRequest, *, require_agent_token: bool = False):
    """Run the handler; return (response, audit lines, names group-checked)."""
    audit: list[dict[str, Any]] = []
    checked: list[str] = []

    def _groups(name: str) -> set[str]:
        checked.append(name)
        return {"developer"}

    resp = _run(
        host_exec(
            req,
            group_resolver=_groups,
            audit_writer=audit.append,
            require_agent_token=require_agent_token,
        )
    )
    return resp, audit, checked


def test_host_exec_gates_on_the_agent_token_identity():
    # Arrange — token says dev-agent; the body says nothing.
    req = _FakeRequest({"argv": ["true"]}, authenticated_agent="dev-agent")
    # Act
    _, _, checked = _call(req)
    # Assert
    assert checked == ["dev-agent"]


def test_host_exec_audits_token_calls_as_agent_token_identity():
    # Arrange
    req = _FakeRequest({"argv": ["true"]}, authenticated_agent="dev-agent")
    # Act
    _, audit, _ = _call(req)
    # Assert
    assert audit[-1]["identity"] == IDENTITY_AGENT_TOKEN


def test_host_exec_accepts_body_caller_matching_the_token():
    # Arrange
    req = _FakeRequest(
        {"argv": ["true"], "caller": "dev-agent"}, authenticated_agent="dev-agent"
    )
    # Act
    resp, _, _ = _call(req)
    # Assert
    assert resp.status_code == 200


def test_host_exec_refuses_body_caller_disagreeing_with_token():
    # Arrange — authenticated as one agent, claiming to be another.
    req = _FakeRequest(
        {"argv": ["true"], "caller": "privileged-agent"},
        authenticated_agent="dev-agent",
    )
    # Act
    resp, _, _ = _call(req)
    # Assert
    assert resp.status_code == 403


def test_host_exec_mismatch_refusal_never_reaches_group_gate():
    # Arrange
    req = _FakeRequest(
        {"argv": ["true"], "caller": "privileged-agent"},
        authenticated_agent="dev-agent",
    )
    # Act
    _, _, checked = _call(req)
    # Assert
    assert checked == []


def test_host_exec_mismatch_refusal_is_written_to_audit_log():
    # Arrange
    req = _FakeRequest(
        {"argv": ["true"], "caller": "privileged-agent"},
        authenticated_agent="dev-agent",
    )
    # Act
    _, audit, _ = _call(req)
    # Assert
    assert audit[-1]["claimed_caller"] == "privileged-agent"


def test_host_exec_allows_legacy_name_claimed_call_by_default():
    # Arrange — an agent started before tokens existed: body claim only.
    req = _FakeRequest({"argv": ["true"], "caller": "dev-agent"})
    # Act
    resp, _, _ = _call(req)
    # Assert
    assert resp.status_code == 200


def test_host_exec_audits_legacy_call_as_name_claimed_identity():
    # Arrange
    req = _FakeRequest({"argv": ["true"], "caller": "dev-agent"})
    # Act
    _, audit, _ = _call(req)
    # Assert
    assert audit[-1]["identity"] == IDENTITY_NAME_CLAIMED


def test_host_exec_audits_unrecognised_token_distinctly_from_no_token():
    # Arrange — a header was sent but resolved to no agent.
    req = _FakeRequest(
        {"argv": ["true"], "caller": "dev-agent"}, agent_token_presented=True
    )
    # Act
    _, audit, _ = _call(req)
    # Assert
    assert audit[-1]["identity"] == IDENTITY_UNRECOGNISED_TOKEN


def test_require_switch_refuses_legacy_name_claimed_call():
    # Arrange
    req = _FakeRequest({"argv": ["true"], "caller": "dev-agent"})
    # Act
    resp, _, _ = _call(req, require_agent_token=True)
    # Assert
    assert resp.status_code == 403


def test_require_switch_still_admits_agent_token_call():
    # Arrange
    req = _FakeRequest({"argv": ["true"]}, authenticated_agent="dev-agent")
    # Act
    resp, _, _ = _call(req, require_agent_token=True)
    # Assert
    assert resp.status_code == 200


def test_require_switch_is_read_from_listen_process_env():
    # Arrange — a real env var, restored afterwards (no monkeypatch).
    prev = os.environ.get(REQUIRE_AGENT_TOKEN_ENV)
    os.environ[REQUIRE_AGENT_TOKEN_ENV] = "1"
    # Act
    try:
        got = require_agent_token_from_env()
    finally:
        if prev is None:
            os.environ.pop(REQUIRE_AGENT_TOKEN_ENV, None)
        else:
            os.environ[REQUIRE_AGENT_TOKEN_ENV] = prev
    # Assert
    assert got is True
