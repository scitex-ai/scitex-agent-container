"""Tests for the safe runtime activity projection."""

import json

import pytest

from scitex_agent_container._listen._activity_projection import (
    activity_projection,
    project_session_id,
)

from ..runtimes.test__codex_activity import THREAD
from ..runtimes.test__codex_activity_binding import _layout
from ..runtimes.test__codex_activity_projection import _promote


def test_activity_uses_only_published_runtime_evidence(tmp_path) -> None:
    # Arrange
    (tmp_path / "heartbeat.json").write_text(
        json.dumps(
            {
                "state": "busy",
                "current_phase": "reviewing",
                "turn_started_at": 90.0,
                "prompt": "private",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "session.jsonl").write_text("progress\n", encoding="utf-8")

    # Act
    result = activity_projection(
        tmp_path,
        runtime_control={"queue": "draining", "secret": "private"},
        now=100.0,
    )

    # Assert
    assert (
        result["phase"]["value"],
        result["operation"]["value"],
        result["turn_elapsed"]["value"],
        result["queue"]["value"],
        result["last_progress"]["source"],
        result["inference"]["state"],
        "private" in repr(result),
    ) == (
        "reviewing",
        "busy",
        10.0,
        "draining",
        "",
        "unknown",
        False,
    )


def test_activity_is_explicitly_unknown_without_authoritative_signals(tmp_path) -> None:
    # Arrange
    expected = {
        "phase",
        "operation",
        "turn_elapsed",
        "last_progress",
        "queue",
        "inference",
        "tool",
        "wait",
        "session_id",
        "session_jsonl_delta_bytes",
        "subagent_jsonl_delta_bytes",
        "last_event_type",
        "last_turn_status",
        "last_error_code",
        "capacity",
    }

    # Act
    result = activity_projection(tmp_path, now=100.0)

    # Assert
    assert (
        set(result),
        {signal["state"] for signal in result.values()},
        "turn-start timestamp" in result["turn_elapsed"]["reason"],
    ) == (expected, {"unknown"}, True)


def test_fenced_native_api_exposes_lifecycle_counters_and_real_event_time(tmp_path):
    # Arrange: the real native writer publishes a validated canonical heartbeat.
    layout = _layout(tmp_path)
    _promote(layout)
    (layout["state"] / "session.jsonl").write_text(
        "unrelated recent old-harness movement\n"
    )

    # Act: project only fenced native event measurements for the GUI contract.
    activity = activity_projection(layout["state"], now=110)

    # Assert: byte-delta work evidence (not counters) is lifecycle evidence;
    # mtime and caps do not infer work. Single beat after promotion has no
    # prior baseline, so both deltas read 0 (observed, honestly zero).
    assert (
        activity["session_id"]["value"],
        activity["session_jsonl_delta_bytes"]["value"],
        activity["session_jsonl_delta_bytes"]["state"],
        activity["subagent_jsonl_delta_bytes"]["value"],
        activity["last_turn_status"]["value"],
        activity["last_progress"]["value"],
        activity["last_progress"]["source"],
        activity["capacity"]["state"],
        activity["capacity"]["value"],
    ) == (
        THREAD,
        0,
        "observed",
        0,
        "complete",
        "1970-01-01T00:00:14+00:00",
        "heartbeat.authoritative_heartbeat.progress_at",
        "unknown",
        None,
    )


@pytest.mark.parametrize(
    "patch,now",
    [
        pytest.param({}, 191, id="expired"),
        pytest.param({"session_id": "another-thread"}, 110, id="wrong-session"),
        pytest.param({"boot_id": "another-owner"}, 110, id="wrong-boot"),
        pytest.param({"writer": "listen-tui-observer"}, 110, id="pane-writer"),
    ],
)
def test_unfenced_or_stale_heartbeat_cannot_publish_native_activity(
    tmp_path, patch, now
):
    # Arrange: previously valid measurements lose their current identity/lease.
    layout = _layout(tmp_path)
    _promote(layout)
    path = layout["state"] / "heartbeat.json"
    beat = json.loads(path.read_text())
    beat.update(patch)
    path.write_text(json.dumps(beat))

    # Act: a live pane or old cache cannot renew native event authority.
    activity = activity_projection(layout["state"], now=now)

    # Assert: all native work-evidence/session/progress are typed UNKNOWN, never zero.
    assert {
        activity[key]["state"]
        for key in (
            "session_id",
            "session_jsonl_delta_bytes",
            "subagent_jsonl_delta_bytes",
            "last_progress",
        )
    } == {"unknown"}


def test_impossible_or_bool_work_deltas_are_unknown_without_private_error_output(
    tmp_path,
):
    # Arrange: a corrupt detail vector and private full error exist beside the cache.
    layout = _layout(tmp_path)
    _promote(layout)
    path = layout["state"] / "heartbeat.json"
    beat = json.loads(path.read_text())
    beat.update(
        session_jsonl_delta_bytes=True,
        subagent_jsonl_delta_bytes="garbage",
        last_error_message="private-error",
    )
    path.write_text(json.dumps(beat))

    # Act: validate the typed delta vector instead of coercing booleans/numbers.
    activity = activity_projection(layout["state"], now=110)

    # Assert: invalid detail cannot become trusted work evidence or leak full errors.
    assert (
        activity["session_jsonl_delta_bytes"]["state"],
        activity["subagent_jsonl_delta_bytes"]["state"],
        "private-error" in repr(activity),
    ) == ("unknown", "unknown", False)


def test_status_session_prefers_actual_native_thread_over_old_hermes_marker(tmp_path):
    # Arrange: the SDK-era marker is stale but native publication is fenced.
    layout = _layout(tmp_path)
    _promote(layout)
    activity = activity_projection(layout["state"], now=110)

    # Act: use the same selection helper as GET /agents/<name> status.
    session = project_session_id("hermes-session-20260930", activity, harness="codex")

    # Assert: status identifies the actual native thread and its provenance.
    assert session == (THREAD, "heartbeat.authoritative_heartbeat.session_id")


def test_unknown_native_session_never_reuses_the_old_harness_marker(tmp_path):
    # Arrange: no fenced native heartbeat currently establishes its thread.
    activity = activity_projection(tmp_path, now=110)

    # Act: choose the native status session without inferring continuity.
    session = project_session_id("hermes-session-20260930", activity, harness="codex")

    # Assert: UNKNOWN is explicit instead of showing another harness's session.
    assert session == (None, "unknown")
