"""Tests for ``action_store`` — SQLite-backed attempt log.

All tests redirect the DB to ``tmp_path`` via ``root=`` so the real
``~/.scitex/agent-container/actions.db`` is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scitex_agent_container.action_store import (
    DEFAULT_DB_FILENAME,
    DEFAULT_SNAPSHOT_MAX_CHARS,
    OUTCOMES,
    _parse_since,
    _truncate_snapshot,
    append_attempt,
    purge_old,
    query,
    stats,
    summarize,
)


@pytest.fixture
def root(tmp_path):
    """Per-test DB root so tests never collide."""
    return tmp_path


def _base_record(**overrides):
    rec = {
        "agent": "alpha",
        "action": "nonce-probe",
        "outcome": "success",
        "elapsed_s": 1.5,
    }
    rec.update(overrides)
    return rec


# ── Schema / init ─────────────────────────────────────────────────────────────


class TestSchema:
    def test_db_file_created_under_root(self, root):
        append_attempt(_base_record(), root=root)
        assert (root / DEFAULT_DB_FILENAME).is_file()

    def test_schema_idempotent_across_calls(self, root):
        append_attempt(_base_record(), root=root)
        append_attempt(_base_record(), root=root)  # second call reuses schema
        rows = query(root=root)
        assert len(rows) == 2


# ── append_attempt ────────────────────────────────────────────────────────────


class TestAppendAttempt:
    def test_minimum_required_record(self, root):
        append_attempt(_base_record(), root=root)
        rows = query(root=root)
        assert len(rows) == 1
        r = rows[0]
        assert r["agent"] == "alpha"
        assert r["action"] == "nonce-probe"
        assert r["outcome"] == "success"
        assert r["elapsed_s"] == pytest.approx(1.5)
        assert r["ts"]  # auto-populated

    def test_explicit_ts_preserved(self, root):
        ts = "2026-04-17T01:00:00+00:00"
        append_attempt(_base_record(ts=ts), root=root)
        rows = query(root=root)
        assert rows[0]["ts"] == ts

    def test_pane_snapshots_are_stored_and_decoded(self, root):
        append_attempt(
            _base_record(
                pane_before={"format": "full", "text": "before"},
                pane_after={"format": "full", "text": "after"},
            ),
            root=root,
        )
        r = query(root=root)[0]
        assert r["pane_before"] == {"format": "full", "text": "before"}
        assert r["pane_after"] == {"format": "full", "text": "after"}

    def test_string_snapshot_is_wrapped(self, root):
        append_attempt(_base_record(pane_before="plain string"), root=root)
        r = query(root=root)[0]
        assert r["pane_before"] == {"format": "full", "text": "plain string"}

    def test_snapshot_truncated_to_cap(self, root):
        big = "x" * (DEFAULT_SNAPSHOT_MAX_CHARS + 500)
        append_attempt(_base_record(pane_before=big), root=root)
        r = query(root=root)[0]
        assert len(r["pane_before"]["text"]) == DEFAULT_SNAPSHOT_MAX_CHARS
        assert r["pane_before"]["truncated"] is True

    def test_extras_round_trip(self, root):
        append_attempt(
            _base_record(extras={"nonce": "abc123", "polls": 4}),
            root=root,
        )
        r = query(root=root)[0]
        assert r["extras"] == {"nonce": "abc123", "polls": 4}

    def test_invalid_record_does_not_raise_or_write(self, root):
        # Missing 'outcome' -> dropped silently (fail-closed).
        append_attempt({"agent": "a", "action": "x", "elapsed_s": 0}, root=root)
        assert query(root=root) == []

    def test_outcomes_are_all_accepted(self, root):
        for o in OUTCOMES:
            append_attempt(_base_record(outcome=o), root=root)
        got = {r["outcome"] for r in query(root=root, limit=100)}
        assert got == set(OUTCOMES)


# ── _truncate_snapshot ────────────────────────────────────────────────────────


class TestTruncateSnapshot:
    def test_none_passes_through(self):
        assert _truncate_snapshot(None) is None

    def test_wrapper_dict_preserved(self):
        snap = {"format": "full", "text": "hi"}
        assert _truncate_snapshot(snap) == snap

    def test_wrapper_dict_text_truncated(self):
        snap = {"format": "full", "text": "x" * 10_000}
        out = _truncate_snapshot(snap, max_chars=100)
        assert len(out["text"]) == 100
        assert out["truncated"] is True

    def test_plain_dict_without_format_dumped(self):
        out = _truncate_snapshot({"context_pct": 42.0})
        assert out["format"] == "json-dump"
        assert "context_pct" in out["text"]

    def test_plain_string_wrapped(self):
        out = _truncate_snapshot("hello")
        assert out == {"format": "full", "text": "hello"}


# ── query / filters ───────────────────────────────────────────────────────────


class TestQueryFilters:
    def _seed(self, root):
        agents = ["alpha", "beta"]
        actions = ["nonce-probe", "compact"]
        outcomes = ["success", "completion_timeout"]
        for i, (a, ac, o) in enumerate(
            [(ag, an, ot) for ag in agents for an in actions for ot in outcomes]
        ):
            append_attempt(
                _base_record(
                    agent=a,
                    action=ac,
                    outcome=o,
                    elapsed_s=i + 1.0,
                    ts=f"2026-04-17T00:00:{i:02d}+00:00",
                ),
                root=root,
            )

    def test_no_filter_returns_all(self, root):
        self._seed(root)
        rows = query(root=root, limit=100)
        assert len(rows) == 8

    def test_filter_by_agent(self, root):
        self._seed(root)
        rows = query(agent="alpha", root=root, limit=100)
        assert all(r["agent"] == "alpha" for r in rows)
        assert len(rows) == 4

    def test_filter_by_action(self, root):
        self._seed(root)
        rows = query(action="compact", root=root, limit=100)
        assert all(r["action"] == "compact" for r in rows)
        assert len(rows) == 4

    def test_filter_by_outcome(self, root):
        self._seed(root)
        rows = query(outcome="completion_timeout", root=root, limit=100)
        assert all(r["outcome"] == "completion_timeout" for r in rows)

    def test_filter_combined(self, root):
        self._seed(root)
        rows = query(
            agent="alpha",
            action="nonce-probe",
            outcome="success",
            root=root,
            limit=100,
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["agent"] == "alpha"
        assert r["action"] == "nonce-probe"
        assert r["outcome"] == "success"

    def test_filter_by_since_absolute(self, root):
        self._seed(root)
        rows = query(since="2026-04-17T00:00:04+00:00", root=root, limit=100)
        # ts lexicographic compare works for ISO strings.
        assert all(r["ts"] >= "2026-04-17T00:00:04+00:00" for r in rows)

    def test_filter_by_since_relative(self, root):
        # Insert a very recent and a very old entry.
        now = datetime.now(timezone.utc)
        append_attempt(
            _base_record(ts=(now - timedelta(seconds=5)).isoformat()),
            root=root,
        )
        append_attempt(
            _base_record(ts=(now - timedelta(days=10)).isoformat()),
            root=root,
        )
        rows_recent = query(since="1h", root=root, limit=100)
        assert len(rows_recent) == 1

    def test_results_are_newest_first(self, root):
        self._seed(root)
        rows = query(root=root, limit=100)
        tss = [r["ts"] for r in rows]
        assert tss == sorted(tss, reverse=True)

    def test_limit_and_offset(self, root):
        self._seed(root)
        page1 = query(root=root, limit=3, offset=0)
        page2 = query(root=root, limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert not (set(r["id"] for r in page1) & set(r["id"] for r in page2))


# ── _parse_since ──────────────────────────────────────────────────────────────


class TestParseSince:
    def test_none(self):
        assert _parse_since(None) is None

    def test_empty_str(self):
        assert _parse_since("") is None

    def test_datetime_object(self):
        dt = datetime(2026, 4, 17, tzinfo=timezone.utc)
        out = _parse_since(dt)
        assert out is not None
        assert out.startswith("2026-04-17")

    def test_relative_hours(self):
        out = _parse_since("1h")
        now = datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(out)
        assert abs((now - parsed).total_seconds() - 3600) < 5

    def test_relative_days(self):
        out = _parse_since("7d")
        now = datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(out)
        assert abs((now - parsed).total_seconds() - 7 * 86400) < 5

    def test_iso_passthrough(self):
        iso = "2026-04-17T01:02:03+00:00"
        assert _parse_since(iso) == iso


# ── stats ─────────────────────────────────────────────────────────────────────


class TestStats:
    def test_stats_groups_by_action_and_outcome(self, root):
        for _ in range(3):
            append_attempt(_base_record(outcome="success", elapsed_s=1.0), root=root)
        append_attempt(
            _base_record(outcome="completion_timeout", elapsed_s=30.0),
            root=root,
        )
        rows = stats(root=root)
        seen = {(r["action"], r["outcome"]): r for r in rows}
        assert seen[("nonce-probe", "success")]["count"] == 3
        assert seen[("nonce-probe", "success")]["mean_elapsed_s"] == pytest.approx(1.0)
        assert seen[("nonce-probe", "completion_timeout")]["count"] == 1

    def test_stats_p95_computed(self, root):
        for elapsed in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0):
            append_attempt(_base_record(elapsed_s=elapsed), root=root)
        rows = stats(root=root)
        r = next(r for r in rows if r["action"] == "nonce-probe")
        assert r["p95_elapsed_s"] == pytest.approx(10.0, rel=0.2)

    def test_stats_filter_by_agent(self, root):
        append_attempt(_base_record(agent="alpha"), root=root)
        append_attempt(_base_record(agent="beta"), root=root)
        rows = stats(agent="alpha", root=root)
        assert len(rows) == 1


# ── summarize ─────────────────────────────────────────────────────────────────


class TestSummarize:
    def test_empty_returns_zero_shape(self, root):
        out = summarize("ghost", root=root)
        assert out["last_action_at"] == ""
        assert out["last_action_name"] == ""
        assert out["counts"] == {}
        assert out["p95_elapsed_s_by_action"] == {}

    def test_populated_shape(self, root):
        for _ in range(2):
            append_attempt(_base_record(elapsed_s=1.0), root=root)
        append_attempt(
            _base_record(outcome="completion_timeout", elapsed_s=30.0),
            root=root,
        )
        out = summarize("alpha", root=root)
        assert out["last_action_name"] == "nonce-probe"
        assert out["last_action_outcome"] == "completion_timeout"
        assert out["counts"]["nonce-probe:success"] == 2
        assert out["counts"]["nonce-probe:completion_timeout"] == 1
        assert "nonce-probe" in out["p95_elapsed_s_by_action"]

    def test_cross_agent_does_not_leak(self, root):
        append_attempt(_base_record(agent="alpha"), root=root)
        append_attempt(_base_record(agent="beta"), root=root)
        out = summarize("alpha", root=root)
        assert out["counts"] == {"nonce-probe:success": 1}


# ── purge_old ─────────────────────────────────────────────────────────────────


class TestPurgeOld:
    def test_deletes_rows_older_than_cutoff(self, root):
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        append_attempt(_base_record(ts=old), root=root)
        append_attempt(_base_record(ts=new), root=root)
        deleted = purge_old(days=30, root=root)
        assert deleted == 1
        rows = query(root=root)
        assert len(rows) == 1
        assert rows[0]["ts"] == new

    def test_purge_with_no_old_rows_returns_zero(self, root):
        append_attempt(_base_record(), root=root)
        deleted = purge_old(days=30, root=root)
        assert deleted == 0


# ── concurrency smoke ────────────────────────────────────────────────────────


class TestConcurrencySmoke:
    """Light check that repeated writes from the same process are
    safe (WAL mode). Not a full multi-process stress test."""

    def test_many_inserts_in_sequence(self, root):
        n = 200
        for i in range(n):
            append_attempt(_base_record(extras={"i": i}, elapsed_s=float(i)), root=root)
        rows = query(root=root, limit=n + 10)
        assert len(rows) == n
