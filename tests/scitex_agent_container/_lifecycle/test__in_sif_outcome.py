"""Tests for :mod:`scitex_agent_container._lifecycle._in_sif_outcome`.

Pins the PR-3 Checkpoint 2 exit-code table + stdout JSON wire
shape. The contract these tests guard:

  ============== ==== =========== ==============================
  ``exit_code``  HTTP ``kind``    Surface
  ============== ==== =========== ==============================
  0              2xx  ``null``    success
  1              -    transport   host listen unreachable
  2              400  bind_unre…  POST /agents preflight miss
  3              400  spec_inva…  POST /agents shape error
  4              409  already_e…  POST /agents name clash
  5              403  acl_deny    POST/DELETE/send/tail
  6              410  startup_f…  DELETE — stillborn agent
  ============== ==== =========== ==============================

A future drift (renaming a kind, repurposing an exit code, or
forgetting to add a row when a new kind ships) trips one of these
tests loudly. AAA + one assert per test (PA-307). No mocks — the
helpers are pure functions over their inputs (PA-306).
"""

from __future__ import annotations

import json

from scitex_agent_container._lifecycle._in_sif_outcome import (
    InSifOutcome,
    build_outcome,
    outcome_to_stdout_json,
    transport_outcome,
)

# ---------------------------------------------------------------------------
# build_outcome — success (HTTP 2xx)
# ---------------------------------------------------------------------------


def test_build_outcome_success_exit_code_is_zero() -> None:
    # Arrange — host returned 200 OK with a structured body.
    body = {"name": "child", "started": True}
    # Act
    outcome = build_outcome(http_status=200, body=body)
    # Assert
    assert outcome.exit_code == 0


def test_build_outcome_success_ok_is_true() -> None:
    # Arrange
    # Act
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Assert
    assert outcome.ok is True


def test_build_outcome_success_kind_is_none() -> None:
    # Arrange — success has no error kind tag.
    # Act
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Assert
    assert outcome.kind is None


def test_build_outcome_success_echoes_http_status() -> None:
    # Arrange
    # Act
    outcome = build_outcome(http_status=201, body={"x": 1})
    # Assert
    assert outcome.http_status == 201


def test_build_outcome_success_echoes_body_in_details() -> None:
    # Arrange — the host body must thread through verbatim so a
    # consumer of the in-SIF CLI sees what the host said.
    body = {"name": "child", "spec_path": "/.../spec.yaml"}
    # Act
    outcome = build_outcome(http_status=200, body=body)
    # Assert
    assert outcome.details == body


# ---------------------------------------------------------------------------
# Exit-code mapping per kind (the contract rows)
# ---------------------------------------------------------------------------


def test_build_outcome_bind_unresolvable_exits_2() -> None:
    # Arrange — PR-1 preflight body shape; only `kind` is contract.
    body = {"error": "...", "kind": "bind_unresolvable", "details": {}}
    # Act
    outcome = build_outcome(http_status=400, body=body)
    # Assert
    assert outcome.exit_code == 2


def test_build_outcome_spec_invalid_exits_3() -> None:
    # Arrange
    body = {"error": "...", "kind": "spec_invalid"}
    # Act
    outcome = build_outcome(http_status=400, body=body)
    # Assert
    assert outcome.exit_code == 3


def test_build_outcome_already_exists_exits_4() -> None:
    # Arrange
    body = {"error": "...", "kind": "already_exists"}
    # Act
    outcome = build_outcome(http_status=409, body=body)
    # Assert
    assert outcome.exit_code == 4


def test_build_outcome_acl_deny_exits_5() -> None:
    # Arrange — PR-3 Checkpoint 1 body shape.
    body = {"error": "ACL deny", "kind": "acl_deny", "reason": "..."}
    # Act
    outcome = build_outcome(http_status=403, body=body)
    # Assert
    assert outcome.exit_code == 5


def test_build_outcome_startup_failed_exits_6() -> None:
    # Arrange — PR-1 STARTUP_FAILED flat-summary shape on DELETE 410.
    body = {
        "name": "child",
        "status": "startup_failed",
        "kind": "startup_failed",
        "phase": "container_creation",
        "details": {"schema_version": 1},
    }
    # Act
    outcome = build_outcome(http_status=410, body=body)
    # Assert
    assert outcome.exit_code == 6


def test_build_outcome_unknown_kind_exits_99() -> None:
    # Arrange — an unrecognised kind must trip a high sentinel so
    # the operator notices the contract has drifted (a new kind
    # was added to the listen surface without a fresh row here).
    body = {"error": "...", "kind": "future_unknown_kind_42"}
    # Act
    outcome = build_outcome(http_status=400, body=body)
    # Assert
    assert outcome.exit_code == 99


# ---------------------------------------------------------------------------
# Body kind threads through to outcome.kind
# ---------------------------------------------------------------------------


def test_build_outcome_kind_field_propagates_from_body() -> None:
    # Arrange — outcome.kind must mirror body["kind"] so JSON
    # consumers can branch on either.
    body = {"error": "...", "kind": "acl_deny"}
    # Act
    outcome = build_outcome(http_status=403, body=body)
    # Assert
    assert outcome.kind == "acl_deny"


def test_build_outcome_body_with_no_kind_is_transport_classed() -> None:
    # Arrange — a host response that lacks the structured kind tag
    # (e.g. an old listen daemon, pre-PR-1) cannot be classified
    # by the contract. Falls through to ``transport`` so the
    # consumer sees a non-classifiable failure clearly.
    body = {"error": "something old, no kind field"}
    # Act
    outcome = build_outcome(http_status=400, body=body)
    # Assert
    assert outcome.kind == "transport"


def test_build_outcome_non_dict_body_is_transport_classed() -> None:
    # Arrange — host returned plain text / HTML / nothing-JSON.
    # Act
    outcome = build_outcome(http_status=502, body="Bad Gateway")
    # Assert
    assert outcome.kind == "transport"


# ---------------------------------------------------------------------------
# transport_outcome — pre-HTTP failure path
# ---------------------------------------------------------------------------


def test_transport_outcome_exits_1() -> None:
    # Arrange
    # Act
    outcome = transport_outcome("connection refused")
    # Assert
    assert outcome.exit_code == 1


def test_transport_outcome_kind_is_transport() -> None:
    # Arrange
    # Act
    outcome = transport_outcome("any reason")
    # Assert
    assert outcome.kind == "transport"


def test_transport_outcome_http_status_is_none() -> None:
    # Arrange — the response never landed, so there is no status.
    # Act
    outcome = transport_outcome("any reason")
    # Assert
    assert outcome.http_status is None


def test_transport_outcome_details_carry_error_message() -> None:
    # Arrange
    reason = "name or service not known"
    # Act
    outcome = transport_outcome(reason)
    # Assert
    assert outcome.details["error"] == reason


def test_transport_outcome_details_include_url_when_given() -> None:
    # Arrange — operator can see WHAT was attempted, not just WHY.
    url = "http://127.0.0.1:7878"
    # Act
    outcome = transport_outcome("conn refused", url=url)
    # Assert
    assert outcome.details["url"] == url


def test_transport_outcome_details_omit_url_when_not_given() -> None:
    # Arrange — caller without a URL gets a minimal details dict.
    # Act
    outcome = transport_outcome("DNS failed")
    # Assert
    assert "url" not in outcome.details


# ---------------------------------------------------------------------------
# outcome_to_stdout_json — wire shape for stdout consumers
# ---------------------------------------------------------------------------


def test_stdout_json_has_ok_field() -> None:
    # Arrange
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert parsed["ok"] is True


def test_stdout_json_has_kind_field() -> None:
    # Arrange
    outcome = build_outcome(
        http_status=403, body={"error": "ACL deny", "kind": "acl_deny", "reason": "x"}
    )
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert parsed["kind"] == "acl_deny"


def test_stdout_json_has_exit_code_field() -> None:
    # Arrange
    outcome = transport_outcome("conn refused")
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert parsed["exit_code"] == 1


def test_stdout_json_has_http_status_field() -> None:
    # Arrange
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert parsed["http_status"] == 200


def test_stdout_json_has_details_field() -> None:
    # Arrange
    body = {"error": "...", "kind": "spec_invalid"}
    outcome = build_outcome(http_status=400, body=body)
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert parsed["details"] == body


def test_stdout_json_is_single_line() -> None:
    # Arrange — compact form so shell pipe consumers (`jq -r ...`)
    # don't need multi-line buffering.
    outcome = build_outcome(http_status=200, body={"deeply": {"nested": "thing"}})
    # Act
    rendered = outcome_to_stdout_json(outcome)
    # Assert — exactly one newline at the end, none in the middle.
    assert rendered.count("\n") == 1


def test_stdout_json_terminates_with_newline() -> None:
    # Arrange
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Act
    rendered = outcome_to_stdout_json(outcome)
    # Assert
    assert rendered.endswith("\n")


def test_stdout_json_has_exactly_five_top_level_fields() -> None:
    # Arrange — pin the wire-shape keys so a future contributor
    # doesn't silently add a debug field that breaks consumers.
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Act
    parsed = json.loads(outcome_to_stdout_json(outcome))
    # Assert
    assert set(parsed.keys()) == {"ok", "kind", "exit_code", "http_status", "details"}


# ---------------------------------------------------------------------------
# Frozen dataclass guard
# ---------------------------------------------------------------------------


def test_outcome_is_frozen_dataclass() -> None:
    # Arrange
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Act — frozen dataclass raises on attribute set.
    raised = False
    try:
        outcome.exit_code = 99  # type: ignore[misc]
    except Exception:
        raised = True
    # Assert
    assert raised is True


def test_outcome_returns_dataclass_instance() -> None:
    # Arrange
    # Act
    outcome = build_outcome(http_status=200, body={"x": 1})
    # Assert
    assert isinstance(outcome, InSifOutcome)
