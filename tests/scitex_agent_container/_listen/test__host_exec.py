"""Tests for the ``POST /v1/host_exec`` handler.

No mocks and no monkeypatching (STX-NM002). The handler exposes two keyword
seams — ``group_resolver`` and ``audit_writer`` — with production defaults, so
each test constructs plain hand-rolled fakes (a lambda for the group resolver;
a list ``append`` for the audit writer) and passes them explicitly. Body
parsing, argv validation, and the "no caller" branch return before the group
seam is touched, so those tests don't need to inject anything.

A tiny ``_FakeRequest`` mirrors only the two Request attributes the handler
uses: ``state.authenticated_node`` and ``.json()``. AAA-marked, one assert per
test.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from typing import Any

from scitex_agent_container._listen._host_exec import (
    ELIGIBLE_GROUPS,
    host_exec,
)


_BAD_BODY = object()


class _FakeRequest:
    def __init__(self, body: object, *, authenticated_node: str | None = None) -> None:
        self._body = body
        self.state = types.SimpleNamespace(authenticated_node=authenticated_node)

    async def json(self) -> object:
        if self._body is _BAD_BODY:
            raise ValueError("no body")
        return self._body


def _run(coro):
    """Async runner that opens a fresh event loop per call so tests don't leak
    state between each other."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _resp_json(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def _noop_audit(_entry: dict[str, Any]) -> None:
    return None


# --------------------------------------------------------------------------
# Body / argv validation — returns 400 before group check, so no seams needed.
# --------------------------------------------------------------------------


def test_host_exec_returns_400_on_malformed_body():
    # Arrange
    req = _FakeRequest(_BAD_BODY, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_argv_missing():
    # Arrange
    req = _FakeRequest({}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_argv_empty_list():
    # Arrange
    req = _FakeRequest({"argv": []}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_argv_is_string_form():
    # Arrange — refuse a shell-form string; argv must be a list.
    req = _FakeRequest({"argv": "echo hi"}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_argv_element_not_string():
    # Arrange
    req = _FakeRequest({"argv": ["echo", 42]}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_timeout_out_of_range():
    # Arrange
    req = _FakeRequest(
        {"argv": ["echo", "hi"], "timeout_s": -1.0},
        authenticated_node="dev",
    )
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_env_is_not_a_mapping():
    # Arrange
    req = _FakeRequest(
        {"argv": ["echo", "hi"], "env": ["not", "a", "map"]},
        authenticated_node="dev",
    )
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


def test_host_exec_returns_400_when_caller_is_not_a_string():
    # Arrange
    req = _FakeRequest(
        {"argv": ["echo", "hi"], "caller": 42}, authenticated_node=None
    )
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# ACL / group gate — inline hand-rolled group_resolver
# --------------------------------------------------------------------------


def test_host_exec_denies_when_no_caller_resolves():
    # Arrange — no per-node bearer and no body claim; refused before the
    # group check even runs.
    req = _FakeRequest({"argv": ["echo", "hi"]}, authenticated_node=None)
    # Act
    resp = _run(host_exec(req))
    # Assert
    assert resp.status_code == 403


def test_host_exec_denies_when_group_is_not_eligible():
    # Arrange — hand-rolled resolver that reports a non-eligible group.
    req = _FakeRequest({"argv": ["echo", "hi"]}, authenticated_node="some-agent")
    # Act
    resp = _run(
        host_exec(
            req,
            group_resolver=lambda name: "generalist",
            audit_writer=_noop_audit,
        )
    )
    # Assert
    assert resp.status_code == 403


def test_host_exec_allows_the_researcher_group():
    # Arrange — researcher is eligible per operator scope.
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="a-researcher")
    # Act
    resp = _run(
        host_exec(
            req,
            group_resolver=lambda name: "researcher",
            audit_writer=_noop_audit,
        )
    )
    # Assert
    assert resp.status_code == 200


def test_host_exec_allows_the_privileged_group():
    # Arrange — privileged is eligible per operator scope (2026-07-02).
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="a-privileged")
    # Act
    resp = _run(
        host_exec(
            req,
            group_resolver=lambda name: "privileged",
            audit_writer=_noop_audit,
        )
    )
    # Assert
    assert resp.status_code == 200


def test_host_exec_denies_unlabeled_privileged_style_caller_helpfully():
    # Arrange — an agent that has NOT been labeled into the privileged group
    # resolves to the ungrouped "" (or any non-eligible group) and is refused
    # with a structured 403 that names the eligible groups.
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="unlabeled-agent")
    # Act
    resp = _run(
        host_exec(
            req,
            group_resolver=lambda name: "",
            audit_writer=_noop_audit,
        )
    )
    # Assert
    assert resp.status_code == 403


def test_host_exec_eligible_groups_set_matches_operator_scope():
    # Arrange
    expected = frozenset({"developer", "researcher", "privileged"})
    # Act
    actual = ELIGIBLE_GROUPS
    # Assert
    assert actual == expected


# --------------------------------------------------------------------------
# Execution — real subprocess, no mocks
# --------------------------------------------------------------------------


def _dev_resolver(name: str) -> str:
    return "developer"


def test_host_exec_returns_zero_exit_on_true_command():
    # Arrange
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["exit_code"] == 0


def test_host_exec_returns_non_zero_exit_on_false_command():
    # Arrange
    req = _FakeRequest({"argv": ["false"]}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["exit_code"] != 0


def test_host_exec_captures_stdout():
    # Arrange
    req = _FakeRequest({"argv": ["echo", "hello"]}, authenticated_node="dev")
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["stdout"].strip() == "hello"


def test_host_exec_marks_timed_out_when_command_exceeds_timeout():
    # Arrange
    req = _FakeRequest(
        {"argv": ["sleep", "5"], "timeout_s": 0.05},
        authenticated_node="dev",
    )
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert _resp_json(resp)["timed_out"] is True


def test_host_exec_returns_500_when_command_not_found():
    # Arrange
    req = _FakeRequest(
        {"argv": ["/nonexistent/binary/definitely-not-here-20260701"]},
        authenticated_node="dev",
    )
    # Act
    resp = _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=_noop_audit))
    # Assert
    assert resp.status_code == 500


# --------------------------------------------------------------------------
# Audit — recording audit_writer
# --------------------------------------------------------------------------


def test_host_exec_calls_audit_writer_exactly_once_on_success():
    # Arrange
    entries: list[dict[str, Any]] = []
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="dev")
    # Act
    _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=entries.append))
    # Assert
    assert len(entries) == 1


def test_host_exec_audit_entry_records_caller_and_group():
    # Arrange
    entries: list[dict[str, Any]] = []
    req = _FakeRequest({"argv": ["true"]}, authenticated_node="named-caller")
    # Act
    _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=entries.append))
    # Assert
    assert (entries[0]["caller"], entries[0]["caller_group"]) == (
        "named-caller",
        "developer",
    )


def test_host_exec_audit_entry_records_argv():
    # Arrange
    entries: list[dict[str, Any]] = []
    req = _FakeRequest({"argv": ["echo", "audit"]}, authenticated_node="dev")
    # Act
    _run(host_exec(req, group_resolver=_dev_resolver, audit_writer=entries.append))
    # Assert
    assert entries[0]["argv"] == ["echo", "audit"]


def test_host_exec_does_not_block_the_event_loop():
    # Arrange — the invariant is "host_exec dispatches its blocking
    # subprocess.run OFF the event loop", i.e. two concurrent execs OVERLAP
    # rather than serialize. A hard wall-clock ceiling (the old `< 0.5s`)
    # flakes under shared-runner load, where subprocess spawn + scheduler
    # overhead is unbounded (observed 0.54s on the CI SIF). So measure a
    # RELATIVE invariant instead: run the same two ~0.3s execs serially, then
    # concurrently. If the loop is NOT blocked, the concurrent run overlaps to
    # ~half the serial time; if host_exec ran subprocess.run ON the loop, the
    # two runs are ~equal. Both measurements absorb the same per-exec
    # overhead, so their RATIO is load-independent where an absolute wall
    # threshold is not.
    def _mk():
        return host_exec(
            _FakeRequest({"argv": ["sleep", "0.3"]}, authenticated_node="dev"),
            group_resolver=_dev_resolver,
            audit_writer=_noop_audit,
        )

    async def _serial() -> float:
        start = time.monotonic()
        await _mk()
        await _mk()
        return time.monotonic() - start

    async def _concurrent() -> float:
        start = time.monotonic()
        await asyncio.gather(_mk(), _mk())
        return time.monotonic() - start

    # Act
    serial = _run(_serial())
    concurrent = _run(_concurrent())

    # Assert — concurrent dispatch is clearly less than serial (they overlap).
    # The 0.75 factor sits well below the ~1.0 ratio a loop-blocking
    # implementation would produce, yet far enough above the ~0.5 ideal to
    # absorb scheduler jitter.
    assert concurrent < serial * 0.75
