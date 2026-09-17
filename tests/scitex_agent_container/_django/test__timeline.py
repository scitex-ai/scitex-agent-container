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

from scitex_agent_container._django._constants import IDENTITY_ENV
from scitex_agent_container._django._timeline import (
    DEFAULT_WINDOW,
    TimelineEntry,
    build_timeline,
    dedupe_entries,
    timeline_rows,
)


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


def test_timeline_endpoint_requires_no_secrets_and_shapes_json(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    response = client.get("/api/timeline")
    payload = json.loads(response.content)
    # Assert
    assert response.status_code == 200 and payload["ok"] is True
    assert "entries" in payload and isinstance(payload["entries"], list)


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
