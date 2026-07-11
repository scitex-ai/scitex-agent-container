"""Tests for the ``sac accounts list`` Stored-accounts renderer.

PA-306 no-mocks: every test exercises real production helpers. The
``env_save_restore`` fixture mutates real ``os.environ`` keys and
auto-reverts (parallel pattern to ``sandbox_home`` in
``test_account_group.py``); no monkeypatching of stdlib internals.

The renderer module is the bullet-1/2/3 fix surface:

* bullet 1 — operator timezone (``SCITEX_AGENT_CONTAINER_TZ`` > ``TZ`` >
  system local).
* bullet 2 — credential TTL ticks under ``watch -n1`` (minute-resolution
  format) and the per-account usage snapshot age is rendered next to
  the % so a stale number is obvious.
* bullet 3 — ``rich.table.Table`` with aligned columns, short
  ``Last Update`` (renamed from ``As-of`` per the 2026-06-09 operator
  ask).
* 2026-07-11 dedupe directive — the table is exactly
  ``Account | Status | Last Update`` (no Email / Plan / 5h% / 7d%
  columns); the compact ``→HH:MM`` / ``→Day HHh`` reset hints render
  in the usage-bars block instead (asserted in
  ``test__account_usage_bars.py``), and the ``(in Xh Ym)`` countdown
  qualifier was dropped together with the table cells it annotated.

The ``--refresh`` flag is asserted via the CLI surface (click invocation)
with a real fake fetcher injected through a temporary monkey of the
``fetch_usage_for_credentials`` symbol the renderer imports at call-time
(the import lives inside ``usage_for_account``, so a single attribute
replace on the ``_account.claude_usage`` module is the honest seam).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_list_render import (
    AccountRow,
    build_stored_rows,
    format_as_of_short,
    format_dt_local,
    format_reset_day_hour,
    format_reset_hhmm,
    format_snapshot_age,
    format_ttl_live,
    local_timezone,
    needs_rolling_legend,
    render_stored_table_to_str,
    rolling_legend_line,
    usage_for_account,
)
from scitex_agent_container.cli_pkg.account_group import account

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``.

    Same shape as ``test_account_group.py::sandbox_home`` so the
    account-store cascade stays in the test's tmpdir. Also clears any
    TZ env that pytest may have inherited from the parent process so
    each test starts from a known precedence baseline.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("TZ")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_TZ")
    return home


# ---------------------------------------------------------------------------
# Bullet 1 — timezone precedence
# ---------------------------------------------------------------------------


def test_local_timezone_no_env_returns_none(env_save_restore):
    # Arrange
    env_save_restore.delete("TZ")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_TZ")
    # Act
    tz = local_timezone()
    # Assert
    assert tz is None


def test_local_timezone_tz_env_resolves(env_save_restore):
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    tz = local_timezone()
    # Assert
    assert tz is not None and "Tokyo" in str(tz)


def test_local_timezone_project_env_wins_over_tz(env_save_restore):
    # Arrange
    env_save_restore.set("TZ", "America/New_York")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_TZ", "Asia/Tokyo")
    # Act
    tz = local_timezone()
    # Assert — project-specific override wins
    assert "Tokyo" in str(tz)


def test_local_timezone_bad_value_falls_through(env_save_restore):
    # Arrange — typo in project env, real TZ valid → real TZ wins
    env_save_restore.set("SCITEX_AGENT_CONTAINER_TZ", "Not/A/Real/Zone")
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    tz = local_timezone()
    # Assert
    assert tz is not None and "Tokyo" in str(tz)


def test_format_dt_local_jst_offset(env_save_restore):
    # Arrange — fixed UTC instant; assert it renders +09:00.
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_dt_local("2026-05-31T12:11:23+00:00")
    # Assert — 12:11 UTC = 21:11 JST.
    assert "21:11:23" in rendered and "+09:00" in rendered


def test_format_dt_local_project_env_wins(env_save_restore):
    # Arrange
    env_save_restore.set("TZ", "America/New_York")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_TZ", "Asia/Tokyo")
    # Act
    rendered = format_dt_local("2026-05-31T12:11:23+00:00")
    # Assert — JST not EST.
    assert "+09:00" in rendered and "-04:00" not in rendered


def test_format_dt_local_none_renders_dash():
    # Arrange — None input.
    value = None
    # Act
    rendered = format_dt_local(value)
    # Assert
    assert rendered == "-"


def test_format_dt_local_unparseable_renders_dash():
    # Arrange — non-ISO string.
    value = "not-a-timestamp"
    # Act
    rendered = format_dt_local(value)
    # Assert
    assert rendered == "-"


# ---------------------------------------------------------------------------
# Bullet 2a — credential TTL must tick under watch -n1
# ---------------------------------------------------------------------------


def test_format_ttl_live_minute_resolution_positive():
    # Arrange — 2.8h = 10080s. 10080 // 3600 = 2h, rem 880s = 14m40s → 2h48m.
    hours = 2.8
    # Act
    rendered = format_ttl_live(hours)
    # Assert
    assert rendered == "+2h48m"


def test_format_ttl_live_negative():
    # Arrange
    hours = -138.6
    # Act
    rendered = format_ttl_live(hours)
    # Assert
    assert rendered == "-138h36m"


def test_format_ttl_live_sub_hour():
    # Arrange — 0.5h = 30 minutes exactly.
    hours = 0.5
    # Act
    rendered = format_ttl_live(hours)
    # Assert
    assert rendered == "+30m00s"


def test_format_ttl_live_sub_minute():
    # Arrange — 1/3600 h = 1 second.
    hours = 1.0 / 3600.0
    # Act
    rendered = format_ttl_live(hours)
    # Assert
    assert rendered == "+1s"


def test_format_ttl_live_ticks_under_60s_elapsed():
    """Two calls 60s apart on the SAME cached snapshot must differ.

    This is the bullet-2 spec: TTL has to tick down when ``watch -n1``
    refreshes the renderer between minutes. The prior ``+2.8h`` format
    collapsed the change; the new ``+2h48m`` format exposes it.
    """
    # Arrange — t0 = 2h48m remaining, t1 = 60s later → 2h47m remaining.
    hours_t0 = (2 * 3600 + 48 * 60) / 3600.0
    hours_t1 = hours_t0 - 60 / 3600.0
    # Act
    s0 = format_ttl_live(hours_t0)
    s1 = format_ttl_live(hours_t1)
    # Assert
    assert s0 != s1, f"60-second tick must change the rendered TTL: {s0} vs {s1}"


def test_format_ttl_live_none_renders_dash():
    # Arrange
    hours = None
    # Act
    rendered = format_ttl_live(hours)
    # Assert
    assert rendered == "-"


# ---------------------------------------------------------------------------
# Bullet 2b — snapshot age column makes stale data obvious
# ---------------------------------------------------------------------------


def test_format_snapshot_age_seconds():
    # Arrange — 30s gap.
    now = datetime(2026, 5, 31, 12, 1, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_snapshot_age("2026-05-31T12:00:30+00:00", now=now)
    # Assert
    assert rendered == "30s"


def test_format_snapshot_age_minutes():
    # Arrange — 3-minute gap.
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_snapshot_age("2026-05-31T12:11:00+00:00", now=now)
    # Assert
    assert rendered == "3m"


def test_format_snapshot_age_hours():
    # Arrange — 1.5-hour gap rounds down to "1h".
    now = datetime(2026, 5, 31, 13, 0, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_snapshot_age("2026-05-31T11:30:00+00:00", now=now)
    # Assert
    assert rendered == "1h"


def test_format_snapshot_age_days():
    # Arrange — 2-day gap.
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_snapshot_age("2026-05-29T12:00:00+00:00", now=now)
    # Assert
    assert rendered == "2d"


def test_format_snapshot_age_unparseable():
    # Arrange
    value = "not-a-time"
    # Act
    rendered = format_snapshot_age(value)
    # Assert
    assert rendered == "?"


def test_format_snapshot_age_future_clamps_to_zero():
    # Arrange — snapshot ts is AFTER now (clock skew); never go negative.
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    # Act
    rendered = format_snapshot_age("2026-05-31T12:00:30+00:00", now=now)
    # Assert
    assert rendered == "0s"


# ---------------------------------------------------------------------------
# Bullet 3 — short Last Update (renamed from As-of) and table shape
# ---------------------------------------------------------------------------


def test_format_as_of_short_day_hour(env_save_restore):
    # Arrange
    env_save_restore.set("TZ", "UTC")
    # Act — Sunday 2026-05-31, 21:00 UTC.
    rendered = format_as_of_short("2026-05-31T21:00:00+00:00")
    # Assert — `Sun 21h` shape.
    assert rendered == "Sun 21h"


def test_format_as_of_short_jst(env_save_restore):
    # Arrange — 12:11 UTC = 21:11 JST → "Sun 21h".
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:00+00:00")
    # Assert
    assert rendered == "Sun 21h"


def test_format_as_of_short_strips_microseconds(env_save_restore):
    """``format_as_of_short`` removes the microsecond component."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:23.756321+00:00")
    # Assert
    assert "." not in rendered


def test_format_as_of_short_strips_offset(env_save_restore):
    """``format_as_of_short`` does not carry the ``+HH:MM`` UTC offset."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:23.756321+00:00")
    # Assert
    assert "+" not in rendered


def test_format_as_of_short_strips_t_separator(env_save_restore):
    """``format_as_of_short`` returns a non-ISO short form (no ``T``)."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:23.756321+00:00")
    # Assert
    assert "T" not in rendered


def test_format_as_of_short_under_8_chars(env_save_restore):
    """``format_as_of_short`` is bounded to the ``Day HHh`` shape (≤8 chars)."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:23.756321+00:00")
    # Assert
    assert len(rendered) <= 8


def _table_row(**overrides) -> AccountRow:
    """Arrange helper: a representative row; kwargs override any field."""
    base: dict = dict(
        name="work",
        freshness_state="VALID",
        freshness_hours=2.8,
        used_pct_5h=42.0,
        used_pct_7d=15.0,
        snapshot_as_of="2026-05-31T12:11:00+00:00",
    )
    base.update(overrides)
    return AccountRow(**base)


def test_render_stored_table_has_column_headers():
    """The 2026-07-11 contract: exactly Account | Status | Last Update."""
    # Arrange — one row.
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — every column header is present.
    for col in ("Account", "Status", "Last Update"):
        assert col in out, f"missing column header: {col!r}\n---\n{out}"


def test_render_stored_table_omits_email_column():
    """Operator directive 2026-07-11: IDs are email-derived slugs — no Email."""
    # Arrange
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "Email" not in out


def test_render_stored_table_omits_plan_column():
    """Operator directive 2026-07-11: Plan is JSON-only — not in the table."""
    # Arrange
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "Plan" not in out


def test_render_stored_table_omits_usage_pct_columns():
    """The bars own the percentages — no 5h%/7d% duplication in the table."""
    # Arrange
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "5h%" not in out and "7d%" not in out


def test_render_stored_table_omits_percentage_cells():
    """A row WITH cached usage still renders no percentage in the table."""
    # Arrange
    rows = [_table_row(used_pct_5h=42.0, used_pct_7d=15.0)]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "42%" not in out and "15%" not in out


def test_render_stored_table_omits_reset_hints():
    """Reset hints render on the bars lines now — never in the table."""
    # Arrange — a row that HAS both reset timestamps cached.
    rows = [
        _table_row(
            reset_at_5h="2026-05-31T12:05:00+00:00",
            reset_at_7d="2026-06-04T08:00:00+00:00",
        ),
    ]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "(→" not in out


_TWO_ROW_TABLE_ROWS = [
    AccountRow(
        name="aa",
        freshness_state="VALID",
        freshness_hours=1.0,
        used_pct_5h=10.0,
        used_pct_7d=2.0,
        snapshot_as_of="2026-05-31T12:00:00+00:00",
    ),
    AccountRow(
        name="bbbb",
        freshness_state="EXPIRED",
        freshness_hours=-5.0,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=None,
    ),
]


def _two_row_table_cell_lines() -> list[str]:
    """Render the canonical 2-row rich table and return non-blank cell lines."""
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    out = render_stored_table_to_str(_TWO_ROW_TABLE_ROWS, now=now)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return [ln for ln in lines if "│" in ln or "┃" in ln]


def test_render_stored_table_emits_header_plus_two_data_rows():
    """Rich draws 1 header row + 2 data rows = ≥3 cell-bearing lines."""
    # Arrange — uses the shared 2-row fixture data above.
    rows = _TWO_ROW_TABLE_ROWS
    # Act
    cell_lines = _two_row_table_cell_lines()
    # Assert
    assert len(cell_lines) >= 3, (
        f"expected ≥3 cell lines for header + 2 rows, got {len(cell_lines)}"
    )
    _ = rows  # silence flake; rows is the input to the helper above


def test_render_stored_table_columns_are_uniformly_separated():
    """Each row uses the same separator count (header ``┃`` + data ``│``)."""
    # Arrange — same 2-row fixture (so a misalignment shows up).
    rows = _TWO_ROW_TABLE_ROWS
    # Act
    cell_lines = _two_row_table_cell_lines()
    sep_counts = {ln.count("│") + ln.count("┃") for ln in cell_lines}
    # Assert — exactly one distinct count means every line aligns.
    assert len(sep_counts) == 1, f"columns mis-aligned: {sep_counts}"
    _ = rows  # silence flake; rows is the input to the helper above


def _as_of_short_form_table_out() -> str:
    """Render a 1-row table whose As-of carries microseconds for short-form tests."""
    rows = [_table_row(snapshot_as_of="2026-05-31T12:11:23.756321+00:00")]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    return render_stored_table_to_str(rows, now=now)


def test_render_stored_table_as_of_uses_short_day_hour_form(env_save_restore):
    """As-of cell renders the JST day-of-week + hour (``Sun 21h``)."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    out = _as_of_short_form_table_out()
    # Assert
    assert "Sun 21h" in out


def test_render_stored_table_as_of_strips_microseconds(env_save_restore):
    """The microsecond payload from the snapshot does NOT leak to the table."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    out = _as_of_short_form_table_out()
    # Assert
    assert "756321" not in out


def test_render_stored_table_shows_snapshot_age_in_last_update():
    """The bullet-2 contract: snapshot age renders in the Last Update cell."""
    # Arrange
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — `(3m)` appears in the Last Update column.
    assert "(3m)" in out


# ---------------------------------------------------------------------------
# 2026-06-09 task — per-window reset hint on 5h% / 7d% cells
# ---------------------------------------------------------------------------


def test_format_reset_hhmm_renders_arrow_hhmm(env_save_restore):
    """``format_reset_hhmm`` emits ``→HH:MM`` in the operator's local tz."""
    # Arrange — 12:05 UTC = 21:05 JST.
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_reset_hhmm("2026-05-31T12:05:00+00:00")
    # Assert
    assert rendered == "→21:05"


def test_format_reset_hhmm_none_returns_empty():
    # Arrange
    value = None
    # Act
    rendered = format_reset_hhmm(value)
    # Assert
    assert rendered == ""


def test_format_reset_hhmm_unparseable_returns_empty():
    """A malformed reset timestamp must NOT crash the table — empty string."""
    # Arrange
    value = "not-a-timestamp"
    # Act
    rendered = format_reset_hhmm(value)
    # Assert
    assert rendered == ""


def test_format_reset_day_hour_renders_arrow_day_hour(env_save_restore):
    """``format_reset_day_hour`` emits ``→Day HHh`` in the operator's local tz."""
    # Arrange — 2026-06-04 08:00 UTC = 2026-06-04 17:00 JST → Thu 17h.
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_reset_day_hour("2026-06-04T08:00:00+00:00")
    # Assert
    assert rendered == "→Thu 17h"


def test_format_reset_day_hour_none_returns_empty():
    # Arrange
    value = None
    # Act
    rendered = format_reset_day_hour(value)
    # Assert
    assert rendered == ""


def test_format_reset_day_hour_unparseable_returns_empty():
    # Arrange
    value = "not-a-timestamp"
    # Act
    rendered = format_reset_day_hour(value)
    # Assert
    assert rendered == ""


# ---------------------------------------------------------------------------
# 2026-07-11 dedupe directive — the reset hint must stay COMPACT (the
# operator's verbatim bar example is ``29% (→09:19)`` / ``66% (→Sun 21h)``).
# The former per-cell ``(in Xh Ym)`` countdown qualifier (P3, op 12866) was
# dropped together with the table cells it annotated, so a FUTURE reset must
# render exactly like a past one: bare ``→HH:MM`` / ``→Day HHh``.
# ---------------------------------------------------------------------------


def test_format_reset_hhmm_future_reset_stays_compact(env_save_restore):
    """A reset far in the future renders the bare ``→HH:MM`` — no countdown."""
    # Arrange — 12:05 UTC = 21:05 JST, a century out from any real clock.
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_reset_hhmm("2126-05-31T12:05:00+00:00")
    # Assert
    assert rendered == "→21:05"


def test_format_reset_day_hour_future_reset_stays_compact(env_save_restore):
    """A far-future reset renders the bare ``→Day HHh`` — no countdown."""
    # Arrange — 08:00 UTC = 17:00 JST, a century out from any real clock.
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_reset_day_hour("2126-06-04T08:00:00+00:00")
    # Assert — bare →Day HHh shape; no ``(in ...)`` qualifier appended.
    assert rendered.startswith("→") and rendered.endswith(" 17h") and "(" not in rendered


def test_needs_rolling_legend_true_when_row_lacks_both_resets():
    """When a row carries neither reset_at, the CLI needs the legend line."""
    # Arrange
    rows = [_table_row(reset_at_5h=None, reset_at_7d=None)]
    # Act
    need = needs_rolling_legend(rows)
    # Assert
    assert need is True


def test_needs_rolling_legend_false_when_every_row_has_resets():
    """Per-line hint already discloses the rolling contract — no legend needed."""
    # Arrange
    rows = [
        _table_row(
            reset_at_5h="2026-05-31T12:05:00+00:00",
            reset_at_7d="2026-06-04T08:00:00+00:00",
        ),
    ]
    # Act
    need = needs_rolling_legend(rows)
    # Assert
    assert need is False


def test_needs_rolling_legend_false_when_no_rows():
    """An empty stored-accounts list never needs the legend."""
    # Arrange
    rows: list[AccountRow] = []
    # Act
    need = needs_rolling_legend(rows)
    # Assert
    assert need is False


def test_rolling_legend_line_explains_both_windows():
    """The legend names BOTH windows so the operator knows what 5h/7d mean."""
    # Arrange
    # (no setup)
    # Act
    legend = rolling_legend_line()
    # Assert
    assert "5h" in legend and "7d" in legend and "rolling" in legend


# ---------------------------------------------------------------------------
# Gripe #1 of the 2026-06-09 operator ask: ``As-of`` was unreadable.
# Pin the renamed header on both sides — the new name MUST appear and
# the old name MUST be gone. Split into one-assert tests so CI red
# names exactly which side regressed (header missing vs. legacy name
# leaked back).
# ---------------------------------------------------------------------------


def _build_last_update_header_inputs() -> tuple[list[AccountRow], datetime]:
    """Arrange helper: build the (rows, now) pair for the header-rename cases."""
    rows = [_table_row()]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    return rows, now


def test_render_stored_table_header_includes_last_update():
    """The renamed ``Last Update`` header is present in the table."""
    # Arrange
    rows, now = _build_last_update_header_inputs()
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "Last Update" in out


def test_render_stored_table_header_omits_legacy_as_of():
    """The legacy ``As-of`` header name does not leak back into output."""
    # Arrange
    rows, now = _build_last_update_header_inputs()
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert
    assert "As-of" not in out


def test_render_stored_table_width_fits_80_cols(env_save_restore):
    """Operator gripe 2026-07-11: the old 7-column table wrapped horribly.

    The deduped 3-column table (Account | Status | Last Update) must
    fit a standard 80-column terminal even with a real-length account
    slug, a multi-character TTL and a day-hour Last-Update cell.
    """
    # Arrange — JST so Last-Update carries the operator's wall clock.
    env_save_restore.set("TZ", "Asia/Tokyo")
    rows = [
        _table_row(
            name="ywatanabe-scitex-ai",
            reset_at_5h="2026-05-31T12:05:00+00:00",
            reset_at_7d="2026-06-04T08:00:00+00:00",
        ),
    ]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now, width=80)
    lines = out.splitlines()
    # Assert — every rendered line must fit in 80 columns.
    over = [(i, len(ln)) for i, ln in enumerate(lines) if len(ln) > 80]
    assert not over, f"lines exceed 80 cols: {over}\n---\n{out}"


# ---------------------------------------------------------------------------
# ``build_stored_rows`` must carry BOTH ``reset_at_5h`` and
# ``reset_at_7d`` through from the per-account ``usage.json`` cache so
# the renderer can show the per-row hint. The Anthropic OAuth usage API
# returns ``resets_at`` for both windows; the cache writer in
# :mod:`._account.claude_usage` persists those. Split per-window so CI
# red names exactly which propagation regressed.
# ---------------------------------------------------------------------------


def _seed_stored_account_with_reset_cache(sandbox_home) -> None:
    """Arrange helper: stage a stored account whose usage.json carries both reset_at_*."""
    save_account("work", {"email_address": "w@x"}, home=sandbox_home)
    accts = sandbox_home / ".scitex" / "agent-container" / "accounts" / "work"
    (accts / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}})
    )
    (accts / "usage.json").write_text(
        json.dumps(
            {
                "used_pct_5h": 42.0,
                "used_pct_7d": 15.0,
                "reset_at_5h": "2026-05-31T12:05:00+00:00",
                "reset_at_7d": "2026-06-04T08:00:00+00:00",
                "fetched_at": "2026-05-31T12:11:00+00:00",
                "as_of": "2026-05-31T12:11:00+00:00",
            }
        )
    )


def test_build_stored_rows_propagates_reset_at_5h_from_cache(sandbox_home):
    """``build_stored_rows`` carries ``reset_at_5h`` from the cache."""
    # Arrange
    _seed_stored_account_with_reset_cache(sandbox_home)
    # Act
    rows = build_stored_rows([{"name": "work", "email_address": "w@x"}])
    # Assert
    assert rows[0].reset_at_5h == "2026-05-31T12:05:00+00:00"


def test_build_stored_rows_propagates_reset_at_7d_from_cache(sandbox_home):
    """``build_stored_rows`` carries ``reset_at_7d`` from the cache."""
    # Arrange
    _seed_stored_account_with_reset_cache(sandbox_home)
    # Act
    rows = build_stored_rows([{"name": "work", "email_address": "w@x"}])
    # Assert
    assert rows[0].reset_at_7d == "2026-06-04T08:00:00+00:00"


# ---------------------------------------------------------------------------
# Bullet 2c — TTL liveness across two real ``build_stored_rows`` calls
# ---------------------------------------------------------------------------


def _stage_account_with_expiry_seconds(
    home: Path, name: str, *, expires_in_s: int
) -> None:
    """Write a real account snapshot with a forward-looking ``expiresAt``.

    expiresAt is stored as unix-ms by claude-code; the freshness reader
    accepts both seconds and ms (treats values > 1e12 as ms). We write
    ms to match the production shape.
    """
    import time

    save_account(name, {"email_address": f"{name}@x"}, home=home)
    accts_dir = home / ".scitex" / "agent-container" / "accounts" / name
    expires_at_ms = int((time.time() + expires_in_s) * 1000)
    (accts_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "expiresAt": expires_at_ms,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        )
    )


def _ttl_tick_pair(sandbox_home) -> tuple[list, list]:
    """Stage one account with ~2h48m runway, read; restage at +60s closer, re-read.

    Exercises the bullet-2 contract end-to-end: ``account_freshness`` re-
    reads ``expiresAt`` and subtracts ``time.time()`` on every call;
    paired with the minute-resolution renderer, the result MUST tick.
    """
    _stage_account_with_expiry_seconds(
        sandbox_home, "work", expires_in_s=2 * 3600 + 48 * 60
    )
    accounts = [{"name": "work", "email_address": "w@x"}]
    rows_t0 = build_stored_rows(accounts)
    _stage_account_with_expiry_seconds(
        sandbox_home, "work", expires_in_s=2 * 3600 + 47 * 60
    )
    rows_t1 = build_stored_rows(accounts)
    return rows_t0, rows_t1


def test_build_stored_rows_first_call_emits_freshness_hours(sandbox_home):
    """First call against a fresh credential reports a numeric TTL."""
    # Arrange — fresh ``$HOME`` (sandbox_home autouse fixture).
    home = sandbox_home
    # Act — stage + read the TTL pair, then take the first read.
    rows_t0, _ = _ttl_tick_pair(home)
    # Assert
    assert rows_t0[0].freshness_hours is not None


def test_build_stored_rows_second_call_emits_freshness_hours(sandbox_home):
    """Second call after a +60s restage still reports a numeric TTL."""
    # Arrange — fresh ``$HOME`` (sandbox_home autouse fixture).
    home = sandbox_home
    # Act — stage + read the TTL pair, then take the second read.
    _, rows_t1 = _ttl_tick_pair(home)
    # Assert
    assert rows_t1[0].freshness_hours is not None


def test_build_stored_rows_60s_apart_delta_about_60s(sandbox_home):
    """Two calls bracketing a 60-second restage yield ~60s of TTL delta."""
    # Arrange — fresh ``$HOME`` (sandbox_home autouse fixture).
    home = sandbox_home
    # Act
    rows_t0, rows_t1 = _ttl_tick_pair(home)
    delta_s = (rows_t0[0].freshness_hours - rows_t1[0].freshness_hours) * 3600
    # Assert
    assert 50 < delta_s < 70, f"expected ~60s TTL delta, got {delta_s:.1f}s"


def test_build_stored_rows_60s_apart_rendered_ttl_differs(sandbox_home):
    """The minute-resolution renderer surfaces the 60s tick as a string change."""
    # Arrange — fresh ``$HOME`` (sandbox_home autouse fixture).
    home = sandbox_home
    # Act
    rows_t0, rows_t1 = _ttl_tick_pair(home)
    s0 = format_ttl_live(rows_t0[0].freshness_hours)
    s1 = format_ttl_live(rows_t1[0].freshness_hours)
    # Assert
    assert s0 != s1, f"rendered TTL did not tick: {s0!r} == {s1!r}"


# ---------------------------------------------------------------------------
# CLI surface — --refresh flag wired through to the renderer
# ---------------------------------------------------------------------------


def test_cli_list_refresh_flag_is_accepted(sandbox_home):
    # Arrange — single stored account.
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--refresh"])
    # Assert
    assert result.exit_code == 0, result.output


def test_cli_list_live_alias_is_accepted(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--live"])
    # Assert
    assert result.exit_code == 0, result.output


def _seed_stale_usage_cache(sandbox_home) -> Path:
    """Stage a stored account with a populated usage.json cache and return its path."""
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    accts_dir = sandbox_home / ".scitex" / "agent-container" / "accounts" / "work"
    (accts_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}})
    )
    cache_file = accts_dir / "usage.json"
    cache_file.write_text(
        json.dumps(
            {
                "used_pct_5h": 99.0,
                "used_pct_7d": 99.0,
                "as_of": "2026-05-31T00:00:00+00:00",
                "fetched_at": "2026-05-31T00:00:00+00:00",
            }
        )
    )
    return cache_file


def test_seed_stale_usage_cache_produces_a_file(sandbox_home):
    """Verifies the shared seed helper actually writes a cache file on disk."""
    # Arrange — fresh ``$HOME`` (sandbox_home autouse fixture).
    home = sandbox_home
    # Act
    cache_file = _seed_stale_usage_cache(home)
    # Assert
    assert cache_file.is_file()


def test_cli_list_refresh_busts_usage_cache(sandbox_home):
    """``--refresh`` deletes the per-account usage.json before render.

    Real on-disk cache + a callable fake injected via the documented
    ``opener`` seam of ``fetch_usage_for_credentials`` would require a
    live OAuth handshake; instead we verify the cache file is gone after
    a ``--refresh`` invocation, which is the observable contract.
    """
    # Arrange — stored account with a stale usage.json cache.
    cache_file = _seed_stale_usage_cache(sandbox_home)
    # Act — invoke renderer through the public helper with refresh=True
    # but disable the network call by yanking the access token (no
    # tokens → fetcher errors out → cache reader fallback runs after the
    # cache was already deleted).
    _ = usage_for_account({"name": "work"}, refresh=True)
    # Assert — the cache file was removed by the refresh path.
    assert not cache_file.exists(), "--refresh must bust the on-disk cache"


# ---------------------------------------------------------------------------
# CLI surface — table rendering for the full list command
# ---------------------------------------------------------------------------


def _seed_full_account(home: Path, name: str, *, email: str) -> None:
    """Write a stored account with a credential snapshot + plan fields."""
    import time

    save_account(name, {"email_address": email}, home=home)
    accts = home / ".scitex" / "agent-container" / "accounts" / name
    (accts / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "expiresAt": int((time.time() + 3 * 3600) * 1000),
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_5x",
                }
            }
        )
    )


def test_cli_list_human_renders_table_columns(sandbox_home):
    # Arrange
    _seed_full_account(sandbox_home, "work", email="w@example.com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert — the 2026-07-11 three-column contract from the rich table.
    for col in ("Account", "Status", "Last Update"):
        assert col in result.output, f"missing column {col!r}:\n{result.output}"


def _seed_account_with_iso_usage_cache(sandbox_home) -> None:
    """Stage a stored account with a deterministic ISO usage.json snapshot."""
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    accts = sandbox_home / ".scitex" / "agent-container" / "accounts" / "work"
    (accts / "usage.json").write_text(
        json.dumps(
            {
                "used_pct_5h": 42.0,
                "used_pct_7d": 15.0,
                "fetched_at": "2026-05-31T12:11:23.756321+00:00",
                "as_of": "2026-05-31T12:11:23.756321+00:00",
            }
        )
    )


def test_cli_list_json_does_not_leak_day_of_week(sandbox_home):
    """``--json`` output must NOT carry the human-renderer's day-of-week."""
    # Arrange
    _seed_account_with_iso_usage_cache(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert — `Sun 21h` is a human-renderer artifact; never in JSON.
    assert "Sun" not in result.output


def test_cli_list_json_usage_as_of_keeps_iso_t_separator(sandbox_home):
    """``--json`` carries through the ISO-8601 ``T`` separator on ``as_of``.

    The usage payload's ``as_of`` ISO string must NOT be reformatted —
    downstream consumers parse it as ISO-8601. We deterministically seed
    the usage cache so the JSON path always has an ``as_of`` to assert on
    (no conditional skip).
    """
    # Arrange
    _seed_account_with_iso_usage_cache(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    usage = payload["stored"][0]["usage"]
    # Assert — usage payload present + carries ISO ``T``. If the renderer
    # ever drops the cache, this assertion fires loudly so we notice.
    as_of = (usage or {}).get("as_of") or ""
    assert "T" in as_of, (
        f"--json must carry through ISO `T` separator on usage.as_of; "
        f"got {as_of!r} (usage={usage!r})"
    )


# ---------------------------------------------------------------------------
# JSON schema stability — header rename + reset-hint feature must NOT mutate
# the machine-readable contract downstream consumers parse.
# ---------------------------------------------------------------------------


def _seed_account_with_full_usage_cache(sandbox_home) -> None:
    """Stage a stored account whose usage.json carries every field the API ships."""
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    accts = sandbox_home / ".scitex" / "agent-container" / "accounts" / "work"
    (accts / "usage.json").write_text(
        json.dumps(
            {
                "used_pct_5h": 42.0,
                "used_pct_7d": 15.0,
                "reset_at_5h": "2026-05-31T12:05:00+00:00",
                "reset_at_7d": "2026-06-04T08:00:00+00:00",
                "fetched_at": "2026-05-31T12:11:00+00:00",
                "as_of": "2026-05-31T12:11:00+00:00",
            }
        )
    )


# ---------------------------------------------------------------------------
# JSON-schema stability under the ``As-of`` → ``Last Update`` rename.
# That rename is HUMAN-RENDER-ONLY: the JSON path must NOT start
# emitting a ``last_update`` key (or anything similar) — downstream
# consumers parse ``as_of``. Split into the snake-case (JSON key
# shape) and title-case (human header shape) assertions so CI red
# names exactly which leak path opened.
# ---------------------------------------------------------------------------


def _stage_cli_list_json_runner(sandbox_home) -> CliRunner:
    """Arrange helper: seed account cache, return a CliRunner ready for the JSON list path."""
    _seed_account_with_full_usage_cache(sandbox_home)
    return CliRunner()


def test_cli_list_json_does_not_emit_snake_case_last_update_key(sandbox_home):
    """The JSON output does not start emitting a ``last_update`` key."""
    # Arrange
    runner = _stage_cli_list_json_runner(sandbox_home)
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert "last_update" not in result.output.lower()


def test_cli_list_json_does_not_leak_title_case_last_update_header(sandbox_home):
    """The human-renderer ``Last Update`` header does not leak into JSON output."""
    # Arrange
    runner = _stage_cli_list_json_runner(sandbox_home)
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert "Last Update" not in result.output


def test_cli_list_json_carries_through_reset_at_5h(sandbox_home):
    """``--json`` exposes ``reset_at_5h`` from the upstream API.

    The renderer reads ``reset_at_5h`` from the per-account
    ``usage.json`` cache for its inline hint; the JSON path must
    carry the SAME key through so JSON consumers can compute their
    own reset display.
    """
    # Arrange
    _seed_account_with_full_usage_cache(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    usage = payload["stored"][0]["usage"]
    # Assert
    assert usage["reset_at_5h"] == "2026-05-31T12:05:00+00:00"


def test_cli_list_json_carries_through_reset_at_7d(sandbox_home):
    """``--json`` exposes ``reset_at_7d`` from the upstream API."""
    # Arrange
    _seed_account_with_full_usage_cache(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    usage = payload["stored"][0]["usage"]
    # Assert
    assert usage["reset_at_7d"] == "2026-06-04T08:00:00+00:00"


def test_cli_list_human_shows_reset_hint_when_cache_has_reset_at(sandbox_home):
    """End-to-end: cache → renderer → operator sees ``42% (→...)``.

    Gripe #2 of the 2026-06-09 task, relocated by the 2026-07-11
    dedupe directive: when the per-account cache carries
    ``reset_at_5h``, the human output must show the inline reset hint
    — now on the usage-bars line rather than a table cell. We don't
    pin a specific local time (tests run on hosts in arbitrary
    timezones) — only the arrow marker, which is the renderer's
    unambiguous reset signal.
    """
    # Arrange — full account + a credentials snapshot so the live
    # fetcher path is taken; with no real OAuth token the fetcher
    # falls back to the cached ``usage.json`` we seed below.
    _seed_account_with_full_usage_cache(sandbox_home)
    accts = sandbox_home / ".scitex" / "agent-container" / "accounts" / "work"
    (accts / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}})
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert — bullet-2 contract: the inline arrow marker reaches the
    # operator. The exact local time depends on the host TZ; assert
    # on the marker shape so the test is timezone-independent.
    assert "(→" in result.output, (
        f"missing inline reset hint in human output:\n{result.output}"
    )
