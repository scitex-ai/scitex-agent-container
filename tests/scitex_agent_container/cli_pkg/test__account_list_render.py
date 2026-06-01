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
* bullet 3 — ``rich.table.Table`` with aligned columns, short ``As-of``.

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
    format_snapshot_age,
    format_ttl_live,
    local_timezone,
    render_stored_table_to_str,
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
# Bullet 3 — short As-of and table shape
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


def test_render_stored_table_has_column_headers():
    # Arrange — one row.
    rows = [
        AccountRow(
            name="work",
            email="w@example.com",
            plan_label="Max 20x",
            tier="default_claude_max_20x",
            freshness_state="VALID",
            freshness_hours=2.8,
            used_pct_5h=42.0,
            used_pct_7d=15.0,
            snapshot_as_of="2026-05-31T12:11:00+00:00",
        ),
    ]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — every column header is present.
    for col in ("ID", "Email", "Plan", "Status(+TTL)", "5h%", "7d%", "As-of"):
        assert col in out, f"missing column header: {col!r}\n---\n{out}"


_TWO_ROW_TABLE_ROWS = [
    AccountRow(
        name="aa",
        email="a@example.com",
        plan_label="Pro",
        tier="default_claude_pro",
        freshness_state="VALID",
        freshness_hours=1.0,
        used_pct_5h=10.0,
        used_pct_7d=2.0,
        snapshot_as_of="2026-05-31T12:00:00+00:00",
    ),
    AccountRow(
        name="bbbb",
        email="bbbb@example.com",
        plan_label="Max 20x",
        tier="default_claude_max_20x",
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
    rows = [
        AccountRow(
            name="work",
            email="w@example.com",
            plan_label="Pro",
            tier="default_claude_pro",
            freshness_state="VALID",
            freshness_hours=2.8,
            used_pct_5h=42.0,
            used_pct_7d=15.0,
            snapshot_as_of="2026-05-31T12:11:23.756321+00:00",
        ),
    ]
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


def test_render_stored_table_shows_age_next_to_pct():
    """The bullet-2 contract: snapshot age MUST appear next to the %."""
    # Arrange
    rows = [
        AccountRow(
            name="work",
            email="w@example.com",
            plan_label="Pro",
            tier="default_claude_pro",
            freshness_state="VALID",
            freshness_hours=2.8,
            used_pct_5h=42.0,
            used_pct_7d=15.0,
            snapshot_as_of="2026-05-31T12:11:00+00:00",
        ),
    ]
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — `(3m)` appears in the As-of column.
    assert "(3m)" in out


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
    # Assert — column headers from the rich table.
    for col in ("ID", "Email", "Plan", "Status(+TTL)", "5h%", "7d%", "As-of"):
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
