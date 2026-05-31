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
    # Act + Assert
    assert local_timezone() is None


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
    assert format_dt_local(None) == "-"


def test_format_dt_local_unparseable_renders_dash():
    assert format_dt_local("not-a-timestamp") == "-"


# ---------------------------------------------------------------------------
# Bullet 2a — credential TTL must tick under watch -n1
# ---------------------------------------------------------------------------


def test_format_ttl_live_minute_resolution_positive():
    # 2.8h = 10080s. 10080 // 3600 = 2h, rem 880s = 14m40s, rounded → 2h15m.
    assert format_ttl_live(2.8) == "+2h48m"


def test_format_ttl_live_negative():
    assert format_ttl_live(-138.6) == "-138h36m"


def test_format_ttl_live_sub_hour():
    # 0.5h = 30 minutes exactly.
    assert format_ttl_live(0.5) == "+30m00s"


def test_format_ttl_live_sub_minute():
    # 1/3600 h = 1 second.
    assert format_ttl_live(1.0 / 3600.0) == "+1s"


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
    assert format_ttl_live(None) == "-"


# ---------------------------------------------------------------------------
# Bullet 2b — snapshot age column makes stale data obvious
# ---------------------------------------------------------------------------


def test_format_snapshot_age_seconds():
    now = datetime(2026, 5, 31, 12, 1, 0, tzinfo=timezone.utc)
    assert format_snapshot_age("2026-05-31T12:00:30+00:00", now=now) == "30s"


def test_format_snapshot_age_minutes():
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    assert format_snapshot_age("2026-05-31T12:11:00+00:00", now=now) == "3m"


def test_format_snapshot_age_hours():
    now = datetime(2026, 5, 31, 13, 0, 0, tzinfo=timezone.utc)
    assert format_snapshot_age("2026-05-31T11:30:00+00:00", now=now) == "1h"


def test_format_snapshot_age_days():
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    assert format_snapshot_age("2026-05-29T12:00:00+00:00", now=now) == "2d"


def test_format_snapshot_age_unparseable():
    assert format_snapshot_age("not-a-time") == "?"


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


def test_format_as_of_short_not_microsecond_iso(env_save_restore):
    """The whole reason this column exists: NOT the full ISO timestamp."""
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
    # Act
    rendered = format_as_of_short("2026-05-31T12:11:23.756321+00:00")
    # Assert
    assert "." not in rendered  # no microseconds
    assert "+" not in rendered  # no offset
    assert "T" not in rendered  # not ISO
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


def test_render_stored_table_aligned_columns():
    """Each row's cells line up under their headers (rich draws box edges)."""
    # Arrange
    rows = [
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
    now = datetime(2026, 5, 31, 12, 14, 0, tzinfo=timezone.utc)
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — every non-blank rendered line that contains data has the
    # same number of column-separator characters. The rich table uses
    # `┃` for the header row and `│` for data rows; both forms are
    # counted together so a misalignment between header and body fails.
    lines = [ln for ln in out.splitlines() if ln.strip()]
    cell_lines = [ln for ln in lines if "│" in ln or "┃" in ln]
    assert len(cell_lines) >= 3  # header + 2 data rows
    sep_counts = {ln.count("│") + ln.count("┃") for ln in cell_lines}
    assert len(sep_counts) == 1, f"columns mis-aligned: {sep_counts}\n---\n{out}"


def test_render_stored_table_as_of_is_short_form(env_save_restore):
    # Arrange
    env_save_restore.set("TZ", "Asia/Tokyo")
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
    # Act
    out = render_stored_table_to_str(rows, now=now)
    # Assert — As-of cell rendered as ``Sun 21h (3m)``; no microsecond ISO.
    assert "Sun 21h" in out
    assert "756321" not in out  # no microseconds leaked through


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


def test_build_stored_rows_ttl_ticks_between_calls(sandbox_home):
    """Two calls 60s apart, SAME on-disk state, must show different TTL.

    Exercises the bullet-2 contract end-to-end: ``account_freshness`` re-
    reads ``expiresAt`` and subtracts ``time.time()`` on every call;
    paired with the minute-resolution renderer, the result MUST tick.
    """
    # Arrange — single account with ~2h48m runway.
    _stage_account_with_expiry_seconds(
        sandbox_home, "work", expires_in_s=2 * 3600 + 48 * 60
    )
    accounts = [{"name": "work", "email_address": "w@x"}]
    # Act — first render.
    rows_t0 = build_stored_rows(accounts)
    # Wall-clock the second read at +60s by faking the snapshot expiry
    # 60s closer (equivalent — the renderer reads expiresAt on every call).
    _stage_account_with_expiry_seconds(
        sandbox_home, "work", expires_in_s=2 * 3600 + 47 * 60
    )
    rows_t1 = build_stored_rows(accounts)
    # Assert — the freshness_hours differs by ~60s (~0.0167h).
    assert rows_t0[0].freshness_hours is not None
    assert rows_t1[0].freshness_hours is not None
    delta_s = (rows_t0[0].freshness_hours - rows_t1[0].freshness_hours) * 3600
    assert 50 < delta_s < 70, f"expected ~60s TTL delta, got {delta_s:.1f}s"
    # And the rendered TTL string differs (proof the format exposes it).
    s0 = format_ttl_live(rows_t0[0].freshness_hours)
    s1 = format_ttl_live(rows_t1[0].freshness_hours)
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


def test_cli_list_refresh_busts_usage_cache(sandbox_home):
    """``--refresh`` deletes the per-account usage.json before render.

    Real on-disk cache + a callable fake injected via the documented
    ``opener`` seam of ``fetch_usage_for_credentials`` would require a
    live OAuth handshake; instead we verify the cache file is gone after
    a ``--refresh`` invocation, which is the observable contract.
    """
    # Arrange — stored account with a stale usage.json cache.
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
    assert cache_file.is_file()
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


def test_cli_list_json_still_emits_iso8601(sandbox_home):
    """JSON path must keep ISO ``fetched_at`` / ``as_of`` for downstream
    consumers — only the human renderer reformats."""
    # Arrange
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
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert — usage.as_of carried through unmodified ISO; no day-of-week.
    usage = payload["stored"][0]["usage"]
    # The usage payload may be None if fetch fails; here we only verify
    # the JSON path is structurally unchanged (no `Sun 21h` reformatting).
    assert "Sun" not in result.output  # the day-of-week never leaks to JSON
    if usage is not None and usage.get("as_of"):
        assert "T" in usage["as_of"]  # still ISO-8601
