"""Official cap identities survive uncertainty without repeat fresh restarts."""

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from scitex_agent_container._account.api_key_liveness import (
    capped_windows,
    classify_status,
)
from scitex_agent_container._account.quota_restart import restart_capped_agents

NOW = 1_700_000_000.0


def _cap_row(*, window="monthly", agents=("running",)):
    """Use the actual official-usage classifier and normalized window producer."""
    reset = datetime.fromtimestamp(NOW + 86400, timezone.utc).isoformat()
    body = json.dumps({"usage": {window: {"percent": 100, "resetsAt": reset}}})
    verdict, reason = classify_status("opencode-go", 200, body, now=NOW)
    return {
        "verdict": verdict,
        "reason": reason,
        "agents": list(agents),
        "capped_windows": capped_windows("opencode-go", body, now=NOW),
    }


def _exercise(tmp_path, stages, *, initial=None):
    path = tmp_path / "quota-restarts.json"
    if initial is not None:
        path.write_text(json.dumps(initial))
    calls, reports = [], []

    def restart(name, **options):
        calls.append((name, options))
        return True

    for offset, slots, running in stages:
        reports.append(
            restart_capped_agents(
                {"checked_at": NOW + offset, "slots": slots},
                state_path=path,
                running_names=running,
                restart=restart,
                now=NOW + offset,
            )
        )
    return calls, reports, json.loads(path.read_text())


@pytest.mark.parametrize("hours", [5, 24, 168, 720, 744])
def test_official_quota_window_cap_retains_reset_and_does_not_outlive_it(hours):
    # Arrange
    reset = datetime.fromtimestamp(NOW + hours * 3600, timezone.utc).isoformat()
    body = json.dumps({"usage": {"rolling": {"percent": 100, "resetsAt": reset}}})
    # Act
    verdict = classify_status("opencode-go", 200, body, now=NOW)
    windows = capped_windows("opencode-go", body, now=NOW)
    expired = capped_windows("opencode-go", body, now=NOW + hours * 3600 + 1)
    # Assert
    assert (verdict[0], windows[0]["resetAt"], expired) == ("exhausted", reset, [])


def test_quota_event_causes_one_fresh_restart_only_for_running_consumers(tmp_path):
    # Arrange
    row = _cap_row(agents=("running", "stopped"))
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"KEY": row}, ["running"]),
            (1, {"KEY": row}, ["running"]),
        ],
    )
    # Assert
    assert (reports[0][0]["status"], reports[1][0]["status"], calls) == (
        "restarted-fresh",
        "already-restarted",
        [("running", {"session_override": "fresh", "drain_timeout_s": 30})],
    )


@pytest.mark.parametrize("gap", ["unknown", "unresolved", "dead", "missing"])
def test_uncertainty_does_not_rearm_same_cap(tmp_path, gap):
    # Arrange
    row = _cap_row()
    gaps = {
        name: {"KEY": {"verdict": name, "agents": ["running"]}}
        for name in ["unknown", "unresolved", "dead"]
    }
    gaps["missing"] = {}
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"KEY": row}, ["running"]),
            (10, gaps[gap], ["running"]),
            (1801, {"KEY": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (1, "already-restarted")


def test_stopped_observation_does_not_rearm_same_cap(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"KEY": row}, ["running"]),
            (10, {"KEY": row}, []),
            (1801, {"KEY": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (1, "already-restarted")


def test_partial_unknown_does_not_turn_handled_subset_into_new_cap(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"A": row, "B": row}, ["running"]),
            (1801, {"A": {"verdict": "unknown"}, "B": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (1, "already-restarted")


def test_window_order_does_not_change_cap_identity(tmp_path):
    # Arrange
    row = _cap_row()
    row["capped_windows"] += _cap_row(window="weekly")["capped_windows"]
    reordered = copy.deepcopy(row)
    reordered["capped_windows"].reverse()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"KEY": row}, ["running"]),
            (1801, {"KEY": reordered}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (1, "already-restarted")


def test_positive_recovery_then_same_window_recap_rearms(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"KEY": row}, ["running"]),
            (10, {"KEY": {"verdict": "alive"}}, ["running"]),
            (1801, {"KEY": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (2, "restarted-fresh")


def test_recovery_of_other_slot_does_not_rearm_unrecovered_cap(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"A": row, "B": row}, ["running"]),
            (10, {"A": {"verdict": "alive"}, "B": {"verdict": "unknown"}}, ["running"]),
            (1801, {"B": row}, ["running"]),
            (1802, {"A": row, "B": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-2][0]["status"], reports[-1][0]["status"]) == (
        2,
        "already-restarted",
        "restarted-fresh",
    )


def test_late_other_cap_is_a_new_event(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"A": row}, ["running"]),
            (1801, {"A": row, "B": row}, ["running"]),
            (3602, {"B": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[1][0]["status"], reports[-1][0]["status"]) == (
        2,
        "restarted-fresh",
        "already-restarted",
    )


@pytest.mark.parametrize("reset", [None, "unparseable", "2020-01-01T00:00:00+00:00"])
def test_unidentified_or_expired_cap_does_not_spend_budget(tmp_path, reset):
    # Arrange
    row = _cap_row()
    row["capped_windows"][0]["resetAt"] = reset
    # Act
    calls, reports, state = _exercise(tmp_path, [(0, {"KEY": row}, ["running"])])
    # Assert
    assert (calls, reports[0][0]["status"], state["history"]) == (
        [],
        "unknown-cap-identity",
        {},
    )


def test_zero_balance_without_reset_identity_is_conservatively_held(tmp_path):
    # Arrange
    row = {
        "verdict": "exhausted",
        "reason": "http-200-zero-balance",
        "agents": ["running"],
        "capped_windows": [],
    }
    # Act
    calls, reports, state = _exercise(tmp_path, [(0, {"KEY": row}, ["running"])])
    # Assert
    assert (calls, reports[0][0]["status"], state["history"]) == (
        [],
        "unknown-cap-identity",
        {},
    )


def test_exact_legacy_fingerprint_is_adopted_without_restarting(tmp_path):
    # Arrange
    row = _cap_row()
    caps = [("KEY", row["reason"], row["capped_windows"])]
    fingerprint = hashlib.sha256(json.dumps(caps, sort_keys=True).encode()).hexdigest()
    initial = {"history": {}, "seen": {"running": fingerprint}}
    # Act
    calls, reports, state = _exercise(
        tmp_path,
        [(0, {"KEY": row}, ["running"])],
        initial=initial,
    )
    # Assert
    assert (calls, reports[0][0]["status"], len(state["cap_events"]["running"])) == (
        [],
        "already-restarted",
        1,
    )


def test_ambiguous_legacy_fingerprint_never_guesses_a_new_event(tmp_path):
    # Arrange
    initial = {"history": {}, "seen": {"running": "f" * 64}}
    # Act
    calls, reports, state = _exercise(
        tmp_path,
        [(0, {"KEY": _cap_row()}, ["running"])],
        initial=initial,
    )
    # Assert
    assert (calls, reports[0][0]["status"], state["history"]) == (
        [],
        "legacy-cap-identity-unknown",
        {},
    )


def test_debounce_still_refuses_another_distinct_event_before_1800s(tmp_path):
    # Arrange
    row = _cap_row()
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [
            (0, {"A": row}, ["running"]),
            (1799, {"A": row, "B": row}, ["running"]),
        ],
    )
    # Assert
    assert (len(calls), reports[-1][0]["status"]) == (1, "debounce")


def test_two_attempts_per_pass_bound_is_unchanged(tmp_path):
    # Arrange
    names = ["a", "b", "c"]
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [(0, {"KEY": _cap_row(agents=names)}, names)],
    )
    # Assert
    assert (len(calls), reports[0][-1]["status"]) == (2, "pass-cap")


def test_two_attempts_per_hour_bound_is_unchanged(tmp_path):
    # Arrange
    initial = {"history": {"running": [NOW - 3500, NOW - 1900]}, "seen": {}}
    # Act
    calls, reports, _state = _exercise(
        tmp_path,
        [(0, {"KEY": _cap_row()}, ["running"])],
        initial=initial,
    )
    # Assert
    assert (calls, reports[0][0]["status"]) == ([], "over-budget")


@pytest.mark.parametrize(
    "verdict,age", [("unknown", 0), ("alive", 0), ("exhausted", 301)]
)
def test_unknown_healthy_or_stale_evidence_never_restarts(tmp_path, verdict, age):
    # Arrange
    document = {
        "checked_at": 1000,
        "slots": {"KEY": {"verdict": verdict, "agents": ["running"]}},
    }
    calls = []
    # Act
    restart_capped_agents(
        document,
        state_path=tmp_path / "state.json",
        running_names=["running"],
        restart=lambda *a, **k: calls.append((a, k)),
        now=1000 + age,
    )
    # Assert
    assert calls == []
