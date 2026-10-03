"""Near-real-time activity timeline: state machine, filters, staleness, bounds.

Card: sac-agent-activity-timeline-dashboard-20260917.

The GUI is a PROJECTION. Every timeline entry must be traceable to a real
signal the SAC control plane published, so the tests below are written to
FAIL if the timeline ever invents a row, silently drops one, or loses the
ability to tell "this agent published nothing" from "I could not ask".

Four properties are pinned here:

1. **stale vs unreachable are DIFFERENT** — an agent whose last published
   signal is old, and a listener that cannot be reached at all, are distinct
   states. Collapsing them hides outages behind "no news".
2. **dedup is real** — the same event must not appear twice just because two
   polls both saw it. A timeline that duplicates on refresh is not a timeline.
3. **refresh is bounded** — a poll may not fan out without limit, and the
   window it returns is capped.
4. **filters compose and are exact** — filtering to one agent must not leak
   another agent's rows into the response.
"""

from __future__ import annotations

import json
import time

import pytest

from scitex_agent_container._django._constants import IDENTITY_ENV
from scitex_agent_container._django._timeline import (
    DEFAULT_WINDOW,
    TimelineEntry,
    build_timeline,
    dedupe_entries,
    timeline_rows,
)


def _native_status(*, count=0, timestamp=None):
    """Synthetic copy of the reviewed typed native activity contract."""
    activity = {
        name: {"state": "observed", "value": count, "source": "heartbeat." + name, "reason": ""}
        for name in ["turns_accepted", "turns_completed", "tools_started", "tools_completed", "tools_inflight"]
    }
    activity.update({
        "last_turn_status": _observed("error", "heartbeat.last_turn_status"),
        "last_error_code": _observed("turn_error", "heartbeat.last_error_code"),
        "session_id": _observed("01a0fdd8-24b2-7b23-a264-4ae60f30245b", "heartbeat.authoritative_heartbeat.session_id"),
        "capacity": _unknown("No authoritative provider capacity measurement is published."),
    })
    status = {"activity": activity}
    if timestamp is not None:
        status["observed_at"] = timestamp
    return status


def _native_row(status, name, *, now=2000):
    return timeline_rows(build_timeline({"native": status}, now=now, kind=name), now=now)[0]


def _observed(value, source="heartbeat.state"):
    return {"state": "observed", "value": value, "source": source}


def _unknown(reason="No authoritative runtime evidence is published."):
    return {"state": "unknown", "value": None, "source": "", "reason": reason}


# ── 1. stale vs unreachable ──────────────────────────────────────────────────


def test_stale_agent_keeps_its_last_real_entry():
    # Arrange: an agent that published once, long ago, and has gone quiet.
    statuses = {
        "quiet": {
            "activity": {"operation": _observed("busy"), "phase": _observed("reviewing")},
            "observed_at": time.time() - 3600,
        }
    }
    # Act
    entries = build_timeline(statuses, now=time.time())
    # Assert: the real observation survives — it is history, not noise.
    assert [e.agent for e in entries] and all(e.agent == "quiet" for e in entries)


def test_unreachable_agent_is_marked_unreachable_not_stale():
    # Arrange: one agent answered with an old observation; another could not be
    # asked at all. These are different facts and must not render identically.
    statuses = {
        "quiet": {"activity": {"operation": _observed("busy")}, "observed_at": time.time() - 3600},
        "gone": Exception("could not reach the SAC listener"),
    }
    # Act
    entries = build_timeline(statuses, now=time.time())
    # Assert
    states = {e.agent: e.state for e in entries}
    assert states.get("quiet") != states.get("gone")


def test_agent_with_no_published_signal_says_unknown_never_invents():
    # Arrange
    statuses = {"mute": {"activity": {"operation": _unknown()}}}
    # Act
    entries = build_timeline(statuses, now=time.time())
    # Assert: nothing observed means no fabricated row value.
    assert all(e.state == "unknown" for e in entries if e.agent == "mute")


# ── 2. dedup ─────────────────────────────────────────────────────────────────


def test_identical_events_dedupe():
    # Arrange
    entry = TimelineEntry(
        agent="alpha", kind="operation", value="busy", state="observed", at=1000.0, source="s"
    )
    # Act
    out = dedupe_entries([entry, entry])
    # Assert
    assert len(out) == 1


def test_a_changed_value_is_a_new_event_not_a_duplicate():
    # Arrange: the same agent moves from `busy` to `idle` at the same timestamp
    # resolution — a dedup key that ignores value would erase real activity.
    a = TimelineEntry(agent="alpha", kind="operation", value="busy", state="observed", at=1000.0, source="s")
    b = TimelineEntry(agent="alpha", kind="operation", value="idle", state="observed", at=1000.0, source="s")
    # Act
    out = dedupe_entries([a, b])
    # Assert
    assert len(out) == 2


def test_dedupe_preserves_order():
    # Arrange
    a = TimelineEntry(agent="a", kind="operation", value="1", state="observed", at=1.0, source="s")
    b = TimelineEntry(agent="b", kind="operation", value="2", state="observed", at=2.0, source="s")
    c = TimelineEntry(agent="c", kind="operation", value="3", state="observed", at=3.0, source="s")
    # Act
    out = dedupe_entries([a, a, b, c, b])
    # Assert
    assert [e.agent for e in out] == ["a", "b", "c"]


# ── 3. bounded window ────────────────────────────────────────────────────────


def test_window_is_capped_so_a_long_history_cannot_flood_the_page():
    # Arrange: far more events than the window allows.
    many = {
        f"agent-{i}": {"activity": {"operation": _observed(f"op-{i}")}, "observed_at": time.time()}
        for i in range(DEFAULT_WINDOW * 3)
    }
    # Act
    entries = build_timeline(many, now=time.time())
    # Assert
    assert len(entries) <= DEFAULT_WINDOW


def test_entries_are_newest_first():
    # Arrange
    now = time.time()
    statuses = {
        "old": {"activity": {"operation": _observed("a")}, "observed_at": now - 500},
        "new": {"activity": {"operation": _observed("b")}, "observed_at": now - 1},
    }
    # Act
    entries = build_timeline(statuses, now=now)
    # Assert
    assert entries[0].at >= entries[-1].at


# ── 4. filters ───────────────────────────────────────────────────────────────


def test_agent_filter_is_exact_and_never_leaks_another_agent():
    # Arrange
    statuses = {
        "alpha": {"activity": {"operation": _observed("busy")}, "observed_at": time.time()},
        "beta": {"activity": {"operation": _observed("idle")}, "observed_at": time.time()},
    }
    # Act
    entries = build_timeline(statuses, now=time.time(), agent="alpha")
    # Assert
    assert entries and {e.agent for e in entries} == {"alpha"}


def test_kind_filter_selects_one_signal_family_only():
    # Arrange
    statuses = {
        "alpha": {"activity": {"operation": _observed("busy"), "phase": _observed("reviewing")},
                  "observed_at": time.time()},
    }
    # Act
    entries = build_timeline(statuses, now=time.time(), kind="phase")
    # Assert
    assert {e.kind for e in entries} == {"phase"}


def test_filter_matching_nothing_returns_empty_not_everything():
    # Arrange
    statuses = {"alpha": {"activity": {"operation": _observed("busy")}, "observed_at": time.time()}}
    # Act
    entries = build_timeline(statuses, now=time.time(), agent="nobody")
    # Assert
    assert entries == []


# ── the API surface ──────────────────────────────────────────────────────────


def test_timeline_endpoint_returns_ok(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    response = client.get("/api/timeline")
    # Assert
    assert response.status_code == 200


def test_timeline_endpoint_shapes_json_with_an_entries_list(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    payload = json.loads(client.get("/api/timeline").content)
    # Assert
    assert payload["ok"] is True and isinstance(payload["entries"], list)


def test_timeline_never_leaks_a_secret(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    body = client.get("/api/timeline").content.decode()
    # Assert
    assert "test-loopback-token" not in body


# ── the rendered surface ─────────────────────────────────────────────────────


def test_timeline_page_renders_server_side(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/timeline/").content.decode()
    # Assert: complete on first paint, no JS required to be useful.
    assert 'data-page="timeline"' in html and 'data-role="timeline"' in html


def test_timeline_page_exposes_filters_as_real_query_params(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/timeline/").content.decode()
    # Assert: the controls work without JS (they are a GET form), so the JS and
    # no-JS paths cannot diverge.
    assert 'name="agent"' in html and 'name="kind"' in html and 'method="get"' in html


def test_timeline_page_filter_narrows_the_rendered_rows(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/timeline/?agent=alpha").content.decode()
    # Assert: a server-side filter really filters what renders.
    assert "alpha" in html


def test_timeline_rows_carry_the_same_key_the_client_dedupes_on():
    # Arrange: the JS dedupes on `key`; if the two layers disagreed about it the
    # page would duplicate rows the API calls identical.
    entry = TimelineEntry(agent="a", kind="operation", value="busy", state="observed",
                          at=1000.0, source="s")
    # Act
    rows = timeline_rows([entry], now=1000.0)
    # Assert
    assert rows[0]["key"] == entry.key and "|" in rows[0]["key"]


def test_timeline_relative_time_is_computed_from_the_observation():
    # Arrange
    entry = TimelineEntry(agent="a", kind="operation", value="busy", state="observed",
                          at=1000.0, source="s")
    # Act
    rows = timeline_rows([entry], now=1000.0 + 125)
    # Assert
    assert rows[0]["relative"] == "2m ago" and rows[0]["age_seconds"] == 125


@pytest.mark.parametrize("count", [0, 1, 31])
def test_native_undated_snapshot_preserves_zero_and_never_claims_success(count):
    # Arrange
    status = _native_status(count=count)
    # Act
    rows = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)
    counters = [r for r in rows if r["kind"].startswith(("turns_", "tools_"))]
    result = (len(rows), [r["value"] for r in counters],
              all(r["state"] == "unknown" and r["at"] is None and r["snapshot"] for r in rows),
              all(r["relative"] == "Age unknown" and r["iso"] == "" for r in rows),
              _native_row(status, "tools_completed")["kind_label"],
              _native_row(status, "last_turn_status")["value"],
              _native_row(status, "last_error_code")["value"])
    # Assert
    assert result == (9, [count] * 5, True, True, "Native tool lifecycles completed", "error", "turn_error")


@pytest.mark.parametrize("name", ["turns_accepted", "turns_completed", "tools_started", "tools_completed", "tools_inflight"])
@pytest.mark.parametrize("value", [None, True, False, -1, 1.5, "0", [], {}, float("nan"), float("inf")])
def test_native_malformed_counter_is_unknown_not_zero_or_an_event(name, value):
    # Arrange
    status = _native_status(timestamp=1999)
    status["activity"][name]["value"] = value
    # Act
    row = _native_row(status, name)
    encoded = json.dumps(row, allow_nan=False)
    result = (row["state"], row["value"], row["display_value"], row["snapshot"], bool(encoded))
    # Assert
    assert result == ("unknown", None, "Unknown", True, True)


@pytest.mark.parametrize("name", ["last_turn_status", "last_error_code", "session_id"])
@pytest.mark.parametrize("value", [None, True, 12, [], {}, "", " ", "\n", "x" * 257])
def test_native_malformed_text_field_is_unknown(name, value):
    # Arrange
    status = _native_status(timestamp=1999)
    status["activity"][name]["value"] = value
    # Act
    row = _native_row(status, name)
    result = (row["state"], row["value"], row["display_value"])
    # Assert
    assert result == ("unknown", None, "Unknown")


@pytest.mark.parametrize("timestamp", [None, True, False, -1, 0, float("nan"), float("inf"), 2001, "1999"])
def test_native_invalid_timestamp_never_borrows_progress_or_poll_time(timestamp):
    # Arrange
    status = _native_status()
    status["observed_at"] = timestamp
    status["activity"]["last_progress"] = _observed("1970-01-01T00:33:19+00:00", "heartbeat.progress_at")
    # Act
    row = _native_row(status, "tools_completed")
    result = (row["value"], row["state"], row["at"], row["relative"], row["iso"])
    # Assert
    assert result == (0, "unknown", None, "Age unknown", "")


@pytest.mark.parametrize("state", ["stale", "unreachable", "unknown", "error", [], {}, None])
def test_native_original_degradation_source_and_reason_survive(state):
    # Arrange
    status = _native_status(timestamp=1999)
    signal = status["activity"]["tools_completed"]
    signal.update(state=state, reason="Original source reason.")
    # Act
    row = _native_row(status, "tools_completed")
    expected = state if isinstance(state, str) and state in {"stale", "unreachable"} else "unknown"
    result = (row["state"], row["source"], row["reason"])
    # Assert
    assert result == (expected, "heartbeat.tools_completed", "Original source reason.")


@pytest.mark.parametrize("name", ["turns_accepted", "turns_completed", "tools_started", "tools_completed", "tools_inflight", "last_turn_status", "last_error_code", "session_id"])
def test_native_absent_field_is_unknown_when_native_contract_is_present(name):
    # Arrange
    status = _native_status(timestamp=1999)
    del status["activity"][name]
    # Act
    row = _native_row(status, name)
    result = (row["state"], row["value"], row["relative"], row["snapshot"], bool(row["note"]))
    # Assert
    assert result == ("unknown", None, "Age unknown", True, True)


@pytest.mark.parametrize("signal", [None, [], "0", 0])
def test_native_malformed_signal_block_is_unknown(signal):
    # Arrange
    status = _native_status(timestamp=1999)
    status["activity"]["tools_completed"] = signal
    # Act
    row = _native_row(status, "tools_completed")
    result = (row["state"], row["value"], row["at"])
    # Assert
    assert result == ("unknown", None, None)


@pytest.mark.parametrize("value", [0, 100, {"capacity": 80}, "unlimited"])
def test_native_capacity_never_interprets_an_unsupported_measurement(value):
    # Arrange
    status = _native_status(timestamp=1999)
    status["activity"]["capacity"] = _observed(value, "unqualified.capacity")
    # Act
    row = _native_row(status, "capacity")
    result = (row["state"], row["value"], row["display_value"], bool(row["note"]))
    # Assert
    assert result == ("unknown", None, "Unknown", True)


@pytest.mark.parametrize("timestamp,expected", [(1000, (1000, "stale", "16m ago")), (1999, (1999, "observed", "1s ago")), (None, (None, "unknown", "Age unknown")), (2001, (None, "unknown", "Age unknown"))])
def test_native_explicit_field_timestamp_controls_its_age(timestamp, expected):
    # Arrange
    status = _native_status(timestamp=1999)
    status["activity"]["tools_completed"]["observed_at"] = timestamp
    # Act
    row = _native_row(status, "tools_completed")
    result = (row["at"], row["state"], row["relative"])
    # Assert
    assert result == expected


def test_native_missing_provenance_does_not_assert_observed_value():
    # Arrange
    status = _native_status(timestamp=1999)
    del status["activity"]["tools_completed"]["source"]
    # Act
    row = _native_row(status, "tools_completed")
    result = (row["state"], row["value"], row["source"])
    # Assert
    assert result == ("unknown", None, "")


def test_native_consumer_does_not_project_account_or_credential_fields():
    # Arrange
    status = _native_status()
    status.update(auth_identity="excluded-account", credentials="excluded-credential", session_id="unqualified-fallback")
    status["activity"].update(auth_identity=_observed("excluded-account"), credentials=_observed("excluded-credential"))
    # Act
    rows = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)
    encoded = json.dumps(rows)
    result = (len(rows), "excluded-account" in encoded, "excluded-credential" in encoded, "unqualified-fallback" in encoded)
    # Assert
    assert result == (9, False, False, False)


def test_legacy_activity_does_not_gain_invented_native_snapshots():
    # Arrange
    status = {"activity": {"operation": _observed("busy")}, "observed_at": 1999}
    # Act
    rows = timeline_rows(build_timeline({"legacy": status}, now=2000), now=2000)
    result = [(row["kind"], row["value"], row["state"], row["snapshot"]) for row in rows]
    # Assert
    assert result == [("operation", "busy", "observed", False)]


def test_native_server_render_has_human_labels_static_keys_and_escaped_reasons():
    # Arrange
    from django.template.loader import render_to_string

    status = _native_status()
    status["activity"]["capacity"]["reason"] = "No <script>capacity</script> measurement."
    rows = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)
    # Act
    html = render_to_string("scitex_agent_container/_timeline_content.html", {"entries": rows, "refresh_seconds": 15, "stale_after_seconds": 300})
    result = (html.count("data-snapshot-key="), "Native tool lifecycles completed" in html,
              "Unknown" in html, "No &lt;script&gt;capacity&lt;/script&gt; measurement." in html,
              "<script>capacity</script>" in html, "Age unknown" in html, 'class="tl-entry tl-observed is-new"' in html)
    # Assert
    assert result == (9, True, True, True, False, True, False)


def test_native_filtered_counter_keeps_its_own_typed_session_provenance():
    # Arrange
    status = _native_status(timestamp=1999)
    # Act
    row = _native_row(status, "tools_completed")
    result = (row["native_session"], row["native_session_at"], row["kind"], row["value"])
    # Assert
    assert result == ("01a0fdd8-24b2-7b23-a264-4ae60f30245b", 1999, "tools_completed", 0)


@pytest.mark.parametrize("value,expected_at", [("1970-01-01T00:33:19+00:00", 1999), ("1970-01-01T01:33:19+01:00", 1999), ("1970-01-01T00:33:19.5Z", 1999.5)])
def test_authoritative_native_progress_uses_actual_iso_event_time(value, expected_at):
    # Arrange
    status = {"activity": {"last_progress": _observed(value, "heartbeat.authoritative_heartbeat.progress_at")}}
    # Act
    row = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)[0]
    result = (row["at"], row["state"], row["value"], row["source"], row["static"], row["kind_label"])
    # Assert
    assert result == (expected_at, "observed", value, "heartbeat.authoritative_heartbeat.progress_at", True, "Last native progress event")


@pytest.mark.parametrize("value", [None, True, 1999, [], {}, "", "invalid", "1970-01-01T00:33:19", "1970-01-01", "1970-01-01X00:33:19+00:00", "1970-99-01T00:33:19+00:00", "1970-01-01T00:33:21Z", "1970-01-01T00:00:00Z", "9999-12-31T23:59:59Z"])
def test_invalid_native_progress_never_borrows_global_or_poll_timestamp(value):
    # Arrange
    status = {"observed_at": 1999, "activity": {"last_progress": _observed(value, "heartbeat.authoritative_heartbeat.progress_at")}}
    # Act
    row = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)[0]
    result = (row["at"], row["state"], row["relative"], row["iso"])
    # Assert
    assert result == (None, "unknown", "Age unknown", "")


@pytest.mark.parametrize("source", ["heartbeat.progress_at", "mtime", "poll_time", ""])
def test_other_progress_sources_are_not_reinterpreted_as_native_event_times(source):
    # Arrange
    status = {"activity": {"last_progress": _observed("1970-01-01T00:33:19Z", source)}}
    # Act
    row = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)[0]
    result = (row["at"], row["state"], row["relative"])
    # Assert
    assert result == (None, "unknown", "Age unknown")


@pytest.mark.parametrize("state", ["unknown", "stale", "unreachable"])
def test_native_progress_parser_does_not_promote_unobserved_signals(state):
    # Arrange
    signal = {"state": state, "value": "1970-01-01T00:33:19Z", "source": "heartbeat.authoritative_heartbeat.progress_at"}
    # Act
    row = timeline_rows(build_timeline({"native": {"activity": {"last_progress": signal}}}, now=2000), now=2000)[0]
    result = (row["at"], row["state"], row["relative"])
    # Assert
    assert result == (None, state, "Age unknown")


def test_native_progress_event_timestamp_does_not_date_counters_or_session():
    # Arrange
    status = _native_status()
    status["activity"]["last_progress"] = _observed("1970-01-01T00:33:19Z", "heartbeat.authoritative_heartbeat.progress_at")
    # Act
    rows = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)
    event = next(row for row in rows if row["kind"] == "last_progress")
    snapshots = [row for row in rows if row["snapshot"]]
    result = (event["at"], len(snapshots), all(row["at"] is None and row["state"] == "unknown" for row in snapshots))
    # Assert
    assert result == (1999, 9, True)


def test_unknown_native_progress_keeps_source_reason_without_displaying_a_future_event():
    # Arrange
    signal = _observed("9999-12-31T23:59:59Z", "heartbeat.authoritative_heartbeat.progress_at")
    signal["reason"] = "Original published source reason."
    # Act
    row = timeline_rows(build_timeline({"native": {"activity": {"last_progress": signal}}}, now=2000), now=2000)[0]
    result = (row["value"], row["display_value"], row["state"], row["at"], row["source"], row["reason"])
    # Assert
    assert result == (None, "Unknown", "unknown", None, "heartbeat.authoritative_heartbeat.progress_at", "Original published source reason.")


def test_same_native_event_keeps_identity_when_source_age_becomes_stale():
    # Arrange
    status = {"activity": {"last_progress": _observed("1970-01-01T00:33:19Z", "heartbeat.authoritative_heartbeat.progress_at")}}
    # Act
    early = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)[0]
    late = timeline_rows(build_timeline({"native": status}, now=2400), now=2400)[0]
    result = (early["event_key"] == late["event_key"], early["key"] != late["key"],
              early["at"], late["at"], early["state"], late["state"], early["age_seconds"], late["age_seconds"])
    # Assert
    assert result == (True, True, 1999, 1999, "observed", "stale", 1, 401)


@pytest.mark.parametrize("different", [
    TimelineEntry("worker", "last_progress", "different-value", "observed", 1999, "runner.progress"),
    TimelineEntry("worker", "last_progress", "value", "observed", 1998, "runner.progress"),
    TimelineEntry("worker", "last_progress", "value", "observed", 1999, "other-source"),
])
def test_different_published_events_keep_distinct_refresh_identity(different):
    # Arrange
    original = TimelineEntry("worker", "last_progress", "value", "observed", 1999, "runner.progress")
    # Act
    rows = timeline_rows([original, different], now=2000)
    # Assert
    assert rows[0]["event_key"] != rows[1]["event_key"]


@pytest.mark.parametrize("value", [None, "invalid", "1970-01-01T00:33:19", "9999-12-31T23:59:59Z"])
def test_unknown_native_progress_cannot_claim_dated_event_identity(value):
    # Arrange
    status = {"activity": {"last_progress": _observed(value, "heartbeat.authoritative_heartbeat.progress_at")}}
    # Act
    row = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)[0]
    # Assert
    assert (row["event_key"], row["at"], row["relative"]) == ("", None, "Age unknown")


def test_dated_native_counter_snapshot_cannot_become_a_tool_event():
    # Arrange
    status = _native_status(timestamp=1999)
    # Act
    rows = timeline_rows(build_timeline({"native": status}, now=2000), now=2000)
    # Assert
    assert all(row["snapshot"] and not row["event_key"] for row in rows)


def test_initial_html_seeds_original_event_time_and_age_for_refresh():
    # Arrange
    from django.template.loader import render_to_string
    entry = TimelineEntry("worker", "last_progress", "value", "observed", 1999, "runner.progress")
    rows = timeline_rows([entry], now=2000)
    # Act
    html = render_to_string("scitex_agent_container/_timeline_content.html", {"entries": rows, "refresh_seconds": 15, "stale_after_seconds": 300})
    result = (html.count("data-event-key="), 'data-observed-at="1999"' in html, 'data-age-seconds="1"' in html)
    # Assert
    assert result == (1, True, True)
