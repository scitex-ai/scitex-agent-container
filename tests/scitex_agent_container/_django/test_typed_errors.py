"""Projection of typed listener errors (status route non-2xx with a `kind`)."""

from __future__ import annotations

from scitex_agent_container._django._projection import project_row
from scitex_agent_container._django._remote import RemoteOperationError


def test_typed_spec_resolution_failure_is_not_unknown():
    exc = RemoteOperationError(400, "Config validation failed for figrecipe/spec.yaml", kind="spec_resolution_failed")
    row = project_row({"name": "figrecipe"}, exc)
    assert row["state_label"] == "Spec invalid"
    assert row["state_tone"] == "warn"
    assert "Config validation failed" in row["state_detail"]


def test_ambiguous_registry_kind_is_surfaced():
    exc = RemoteOperationError(409, "two registries claim this name", kind="ambiguous_registry")
    row = project_row({"name": "x"}, exc)
    assert row["state_label"] == "Ambiguous registry"
    assert row["state_tone"] == "warn"


def test_plain_exception_without_kind_is_unknown():
    row = project_row({"name": "x"}, Exception("boom"))
    assert row["state_label"] == "Unknown"
    assert row["state_tone"] == "warn"
    assert row["state_detail"] == ""
