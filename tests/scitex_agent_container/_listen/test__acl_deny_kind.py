"""Tests for the PR-3 ``acl_deny`` kind in the ACL deny response.

Pins the 5-kind wire-shape contract clew launcher consumes (the 5th
kind joining ``bind_unresolvable``, ``spec_invalid``, ``already_exists``,
``startup_failed`` from PR-1):

  POST /agents 403:
      {"error": "ACL deny", "kind": "acl_deny", "reason": "..."}

  DELETE /agents/<name> 403 (PR-3 lineage gate, future surface):
      {"error": "ACL deny", "kind": "acl_deny", "reason": "..."}

  POST /agents/<name>/message:send 403:
      {"error": "ACL deny", "kind": "acl_deny", "reason": "..."}

  GET /agents/<name>/tail 403 (PR-3 lineage gate, future surface):
      {"error": "ACL deny", "kind": "acl_deny", "reason": "..."}

These tests exercise the ``deny_response`` helper directly so the
wire shape is guaranteed across every consumer (POST /agents,
node_message_send, the new DELETE + tail gates PR-3 adds). AAA +
one assert per test (PA-307). No mocks (PA-306) — the helper is a
pure function over its inputs.
"""

from __future__ import annotations

import json

from scitex_agent_container._listen._acl import deny_response

# ---------------------------------------------------------------------------
# Wire shape — fields + status
# ---------------------------------------------------------------------------


def test_deny_response_returns_http_403() -> None:
    # Arrange — any reason string.
    # Act
    response = deny_response("child caller may not spawn")
    # Assert
    assert response.status_code == 403


def test_deny_response_body_has_kind_acl_deny() -> None:
    # Arrange
    # Act
    response = deny_response("any reason")
    body = json.loads(response.body)
    # Assert
    assert body["kind"] == "acl_deny"


def test_deny_response_body_carries_error_field() -> None:
    # Arrange
    # Act
    response = deny_response("any reason")
    body = json.loads(response.body)
    # Assert
    assert body["error"] == "ACL deny"


def test_deny_response_body_echoes_reason() -> None:
    # Arrange — the reason text must thread through so the operator/
    # MCP host can render the cause to the human.
    reason = "caller 'child-a' has no lineage-permitted access to target 'unrelated-b'"
    # Act
    response = deny_response(reason)
    body = json.loads(response.body)
    # Assert
    assert body["reason"] == reason


# ---------------------------------------------------------------------------
# Kind override — allows future ACL phases to shade the taxonomy
# ---------------------------------------------------------------------------


def test_deny_response_kind_override_replaces_default() -> None:
    # Arrange — a future caller can swap kind for a more specific tag
    # while keeping the 5-kind contract intact (e.g. a sub-taxonomy).
    # Act
    response = deny_response("reason", kind="acl_deny_per_spec")
    body = json.loads(response.body)
    # Assert
    assert body["kind"] == "acl_deny_per_spec"


def test_deny_response_default_kind_when_not_overridden() -> None:
    # Arrange — the default must be the freezable contract value.
    # Act
    response = deny_response("reason")
    body = json.loads(response.body)
    # Assert
    assert body["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# Body has all three contract fields (no extras leaked, no missing)
# ---------------------------------------------------------------------------


def test_deny_response_body_has_exactly_the_three_contract_fields() -> None:
    # Arrange — pin the body keys so a future contributor doesn't
    # silently drift the shape (e.g. by adding a debug field).
    # Act
    response = deny_response("any reason")
    body = json.loads(response.body)
    # Assert
    assert set(body.keys()) == {"error", "kind", "reason"}
