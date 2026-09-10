"""Projection tests: the /agents row + /status merge into the safe dashboard shape."""

from __future__ import annotations

from .conftest import STATUS


def test_project_row_uses_status_liveness_verdict():
    from scitex_agent_container._django._projection import project_row

    from .conftest import LOCAL_NAME

    row = project_row({"name": "alpha", "turn_url": f"http://{LOCAL_NAME}:19000/v1/turn"}, STATUS["alpha"])
    assert row["state_label"] == "Alive"
    assert row["state_tone"] == "good"
    assert row["runtime"] == "apptainer"
    assert row["harness"] == "anthropic"
    assert row["model"] == "sonnet"
    assert row["host"] == LOCAL_NAME
    assert row["cross_host"] is False


def test_project_row_dead_agent_is_bad_tone():
    from scitex_agent_container._django._projection import project_row

    row = project_row({"name": "beta"}, STATUS["beta"])
    assert row["state_label"] == "Dead"
    assert row["state_tone"] == "bad"


def test_project_row_missing_status_is_unknown():
    from scitex_agent_container._django._projection import project_row

    row = project_row({"name": "ghost"}, {})
    assert row["state_label"] == "Unknown"
    assert row["state_tone"] == "warn"


def test_project_row_status_exception_is_unknown_not_crash():
    from scitex_agent_container._django._projection import project_row

    row = project_row({"name": "alpha"}, Exception("boom"))
    assert row["state_label"] == "Unknown"
    assert row["name"] == "alpha"


def test_project_detail_includes_session_short_and_started():
    from scitex_agent_container._django._projection import project_detail

    d = project_detail({"name": "alpha", "started_at": "T0"}, STATUS["alpha"])
    assert d["session"] == "a" * 8  # shortened
    assert d["detail"]["started_at"] == "T0"
    assert "workdir" not in d["detail"]  # operator host path is NOT exposed


def test_project_detail_status_failure_is_explicit():
    from scitex_agent_container._django._projection import project_detail

    d = project_detail({"name": "alpha"}, Exception("endpoint down"))
    assert d["detail_error"]
    assert d["session"] == "—"


def test_role_list_is_joined():
    from scitex_agent_container._django._projection import project_row

    row = project_row({"name": "x", "role": ["a", "b"]}, STATUS["alpha"])
    assert row["role"] == "a, b"


def test_cross_host_row_is_tagged():
    from scitex_agent_container._django._projection import project_row

    from .conftest import REMOTE_NAME

    row = project_row(
        {"name": "gamma", "scope": "cross-host",
         "turn_url": f"http://{REMOTE_NAME}:19002/v1/turn"},
        STATUS["gamma"],
    )
    assert row["cross_host"] is True
    assert row["host"] == REMOTE_NAME
