"""Uuid-aware name resolution: one row resolves, stale rows hint, dual-live refuses.

Card ``sac-identity-spec-uuid-20261026``. The resolver is pure over row
dicts (the caller-facing shape), so these tests inject rows — no store,
no clock beyond an explicit ``now``. House style: AAA markers, one
assertion per test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scitex_agent_container._state.state_store_instances import (
    last_known_instance,
    record_instance_start,
)
from scitex_agent_container._state.state_store_instances_resolve import (
    DualLiveClaimants,
    resolve_live_by_name,
)

_NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc).timestamp()
_FRESH_BEAT = "2026-10-26T07:58:00Z"
_STALE_BEAT = "2026-10-20T07:00:00Z"


def _row(
    name: str,
    host: str,
    started: str,
    *,
    beat: str | None = None,
    spec_uuid: str | None = None,
    row_id: str = "id-1",
) -> dict:
    return {
        "id": row_id,
        "host": host,
        "name": name,
        "started_at": started,
        "last_heartbeat_at": beat,
        "spec_uuid": spec_uuid,
    }


def test_empty_name_resolves_nothing() -> None:
    # Arrange — no name is not a lookup.
    # Act
    row, hint = resolve_live_by_name("", rows=[], now=_NOW)
    # Assert
    assert (row, hint) == (None, None)


def test_unknown_name_resolves_nothing() -> None:
    # Arrange — a name the store never saw.
    rows = [_row("other", "h1", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT)]
    # Act
    row, hint = resolve_live_by_name("ghost", rows=rows, now=_NOW)
    # Assert
    assert (row, hint) == (None, None)


def test_single_live_row_resolves_without_hint() -> None:
    # Arrange — the uncontended case stays silent.
    rows = [_row("solo", "h1", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT)]
    # Act
    row, hint = resolve_live_by_name("solo", rows=rows, now=_NOW)
    # Assert
    assert (row["host"], hint) == ("h1", None)


def test_newest_wins_over_stale_same_name_rows() -> None:
    # Arrange — the live-paper shape: one live row, stale shadows.
    rows = [
        _row("lp", "h-stale", "2026-10-20T06:00:00Z", beat=_STALE_BEAT),
        _row("lp", "h-live", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT),
    ]
    # Act
    row, _hint = resolve_live_by_name("lp", rows=rows, now=_NOW)
    # Assert
    assert row["host"] == "h-live"


def test_stale_shadow_emits_exact_cleanup_command() -> None:
    # Arrange — same collision; the operator needs the next command.
    rows = [
        _row("lp", "h-stale", "2026-10-20T06:00:00Z", beat=_STALE_BEAT),
        _row("lp", "h-live", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT),
    ]
    # Act
    _row_live, hint = resolve_live_by_name("lp", rows=rows, now=_NOW)
    # Assert
    assert "`sac store clean`" in (hint or "") and "h-stale" in (hint or "")


def test_diverged_spec_uuid_is_named_in_hint() -> None:
    # Arrange — same name, different identities: a fork, not leftovers.
    rows = [
        _row("lp", "h-stale", "2026-10-20T06:00:00Z", spec_uuid="uuid-old"),
        _row("lp", "h-live", "2026-10-26T06:00:00Z", spec_uuid="uuid-new"),
    ]
    # Act
    _row_live, hint = resolve_live_by_name("lp", rows=rows, now=_NOW)
    # Assert
    assert "DIVERGED" in (hint or "")


def test_shared_spec_uuid_reads_as_leftovers() -> None:
    # Arrange — same identity everywhere: restart residue, not a fork.
    rows = [
        _row("lp", "h-stale", "2026-10-20T06:00:00Z", spec_uuid="uuid-same"),
        _row("lp", "h-live", "2026-10-26T06:00:00Z", spec_uuid="uuid-same"),
    ]
    # Act
    _row_live, hint = resolve_live_by_name("lp", rows=rows, now=_NOW)
    # Assert
    assert "same spec_uuid=uuid-same" in (hint or "")


def test_unstamped_rows_ask_for_the_backfill() -> None:
    # Arrange — pre-backfill rows cannot be keyed on identity yet.
    rows = [
        _row("lp", "h-stale", "2026-10-20T06:00:00Z"),
        _row("lp", "h-live", "2026-10-26T06:00:00Z"),
    ]
    # Act
    _row_live, hint = resolve_live_by_name("lp", rows=rows, now=_NOW)
    # Assert
    assert "UNSTAMPED" in (hint or "")


def test_two_fresh_claimants_refuse_fail_closed() -> None:
    # Arrange — two hosts both beating for one name: unroutable.
    rows = [
        _row("lp", "h-a", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT),
        _row("lp", "h-b", "2026-10-26T06:01:00Z", beat=_FRESH_BEAT),
    ]
    act = lambda: resolve_live_by_name("lp", rows=rows, now=_NOW)  # noqa: E731
    # Act
    with pytest.raises(DualLiveClaimants) as error:
        act()
    # Assert
    assert len(error.value.claimants) == 2


def test_refusal_names_every_claimant_host() -> None:
    # Arrange — the refusal must carry the evidence, not just the verdict.
    rows = [
        _row("lp", "h-a", "2026-10-26T06:00:00Z", beat=_FRESH_BEAT),
        _row("lp", "h-b", "2026-10-26T06:01:00Z", beat=_FRESH_BEAT),
    ]
    act = lambda: resolve_live_by_name("lp", rows=rows, now=_NOW)  # noqa: E731
    # Act
    with pytest.raises(DualLiveClaimants) as error:
        act()
    # Assert
    assert "h-a" in str(error.value) and "h-b" in str(error.value)


def test_spec_uuid_declared_on_both_schema_surfaces() -> None:
    # Arrange — the plugin declaration and the opened schema must agree
    # field-for-field; a gap in either direction is what the store move
    # measured and closed in both places at once.
    from scitex_agent_container._state.state_store_instances_store import (
        instances_schema,
    )
    from scitex_agent_container._store_plugin import INSTANCES

    # Act
    plugin_fields = set(INSTANCES.fields.keys())
    store_fields = set(instances_schema().fields.keys())
    # Assert
    assert (plugin_fields ^ store_fields) == set()


def test_start_with_spec_uuid_persists_on_row(pg_schema: str) -> None:
    # Arrange — the start path threads the stamped identity to the row.
    record_instance_start(
        "uuid-agent", host="host-a", spec_uuid="uuid-row-1"
    )
    # Act
    row = last_known_instance("uuid-agent")
    # Assert
    assert row["spec_uuid"] == "uuid-row-1"


def test_start_without_spec_uuid_leaves_field_null(pg_schema: str) -> None:
    # Arrange — pre-backfill / peer rows stamp nothing, not a blank.
    record_instance_start("plain-agent", host="host-a")
    # Act
    row = last_known_instance("plain-agent")
    # Assert
    assert row["spec_uuid"] is None
