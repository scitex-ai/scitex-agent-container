"""A refresh must never manufacture the age of runtime evidence."""

from __future__ import annotations

import json

import pytest
from django.template.loader import render_to_string

from scitex_agent_container._django._constants import IDENTITY_ENV
from scitex_agent_container._django._timeline import (
    build_timeline,
    dedupe_entries,
    timeline_rows,
)


def _status(*, state="observed", timestamp=None):
    status = {
        "status": "running",
        "liveness": {"verdict": "alive"},
        "inbox_subscribers": 1,
        "activity": {
            "operation": {
                "state": state,
                "value": "ready",
                "source": "heartbeat.state",
            }
        },
    }
    if timestamp is not None:
        status["observed_at"] = timestamp
    return status


def test_missing_timestamp_is_unknown_age_and_stable_across_polls():
    # Arrange
    statuses = {"live-process": _status()}
    # Act
    first = build_timeline(statuses, now=1000)
    second = build_timeline(statuses, now=2000)
    row = timeline_rows(second, now=2000)[0]
    result = (first[0].at, second[0].at, first[0].state, second[0].state,
              first[0].value, first[0].key == second[0].key,
              len(dedupe_entries(first + second)), row["relative"], row["age_seconds"], row["iso"])
    # Assert
    assert result == (None, None, "unknown", "unknown", "ready", True, 1, "Age unknown", None, "")


@pytest.mark.parametrize("timestamp", [True, False, float("nan"), float("inf"), -1, 0, 2001, "1999"])
def test_invalid_or_future_timestamp_cannot_assert_current_activity(timestamp):
    # Arrange
    status = _status(timestamp=timestamp)
    # Act
    entries = build_timeline({"live-process": status}, now=2000)
    encoded = json.dumps(timeline_rows(entries, now=2000), allow_nan=False)
    result = (entries[0].at, entries[0].state, bool(encoded))
    # Assert
    assert result == (None, "unknown", True)


def test_actual_old_timestamp_stays_stale_with_original_age_and_value():
    # Arrange
    status = _status(timestamp=1000)
    # Act
    entries = build_timeline({"live-process": status}, now=2000)
    row = timeline_rows(entries, now=2000)[0]
    result = (row["at"], row["state"], row["value"], row["source"], row["age_seconds"], row["relative"])
    # Assert
    assert result == (1000, "stale", "ready", "heartbeat.state", 1000, "16m ago")


def test_actual_current_timestamp_is_observed_without_poll_time_substitution():
    # Arrange
    status = _status(timestamp=1999)
    # Act
    entries = build_timeline({"live-process": status}, now=2000)
    row = timeline_rows(entries, now=2000)[0]
    result = (row["at"], row["state"], row["relative"])
    # Assert
    assert result == (1999, "observed", "1s ago")


@pytest.mark.parametrize("state", ["stale", "unreachable"])
def test_known_source_state_is_not_promoted_or_dropped(state):
    # Arrange
    status = _status(state=state, timestamp=1999)
    # Act
    entry = build_timeline({"live-process": status}, now=2000)[0]
    result = (entry.state, entry.value, entry.source)
    # Assert
    assert result == (state, "ready", "heartbeat.state")


@pytest.mark.parametrize("state", ["unknown", "error", "working"])
def test_unknown_or_unsupported_source_state_cannot_invent_error_or_work(state):
    # Arrange
    status = _status(state=state, timestamp=1999)
    # Act
    entry = build_timeline({"live-process": status}, now=2000)[0]
    result = (entry.state, entry.value, entry.source)
    # Assert
    assert result == ("unknown", None, "")


@pytest.mark.parametrize("state", [[], {}, ["observed"], {"state": "observed"}, None, False, 1, 1.5])
def test_malformed_json_signal_state_degrades_to_unknown_without_raising(state):
    # Arrange
    status = _status(state=state, timestamp=1999)
    # Act
    entries = build_timeline({"live-process": status}, now=2000)
    row = timeline_rows(entries, now=2000)[0]
    encoded = json.dumps(row, allow_nan=False)
    result = (row["state"], row["value"], row["source"], bool(encoded))
    # Assert
    assert result == ("unknown", None, "", True)


def test_undated_render_has_no_machine_timestamp_or_just_now_claim():
    # Arrange
    entries = timeline_rows(build_timeline({"live-process": _status()}, now=2000), now=2000)
    # Act
    html = render_to_string("scitex_agent_container/_timeline_content.html", {
        "entries": entries, "refresh_seconds": 15, "stale_after_seconds": 300,
    })
    result = ("Age unknown" in html, "0s ago" in html, '<time class="tl-when"' in html,
              "Published signals from the SAC control plane" in html)
    # Assert
    assert result == (True, False, False, True)


def test_real_api_returns_the_same_stable_presentation_keys_as_initial_html(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    first = client.get("/api/timeline?agent=alpha").json()["entries"]
    second = client.get("/api/timeline?agent=alpha").json()["entries"]
    html = client.get("/timeline/?agent=alpha").content.decode()
    valid = all(row["state"] == "unknown" and row["at"] is None and
                row["age_seconds"] is None and row["relative"] == "Age unknown" and
                row["iso"] == "" and row["state_label"] == "unknown" and
                f'data-key="{row["key"]}"' in html for row in first)
    result = (bool(first), first == second, valid)
    # Assert
    assert result == (True, True, True)
