"""Every externally-derived Rich cell is literal text, never markup."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console

from scitex_agent_container.cli_pkg._account_list_render import (
    AccountRow,
    render_stored_table,
)
from scitex_agent_container.cli_pkg._helpers._agent_list import print_agent_list
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_model import (
    UNREACHABLE,
    FleetListing,
    HostReport,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_render import (
    print_fleet_header,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_render import (
    _narrow_detail_lines,
)
from scitex_agent_container.cli_pkg._helpers._console import console


def _hostile(field: str) -> str:
    return f"[{field}[link=https://evil.invalid]click[/link]"


def _controlled(field: str) -> str:
    return (
        f"{field}\x00\x07\x1b]8;;https://evil.invalid\x07click"
        f"\x1b]8;;\x07\x1b[31mred\x1b[0m\n\t\x9b31mc1\x9b0m"
    )


def _assert_no_terminal_controls(rendered: str) -> None:
    assert "\x1b" not in rendered and "\x07" not in rendered
    assert not any(ord(char) < 32 and char not in "\n\r" for char in rendered)
    assert not any(127 <= ord(char) <= 159 for char in rendered)


def test_agent_table_renders_every_external_cell_literally(capsys) -> None:
    # Arrange
    row = {
        "name": _hostile("name"),
        "status": "running",
        "started_at": "-",
        "host_display": _hostile("host"),
        "billing_mode": _hostile("billing"),
        "auth_identity": _hostile("auth"),
        "harness": _hostile("harness"),
        "engine": _hostile("engine"),
        "model": _hostile("model"),
        "stored_credential": _hostile("credential"),
        "runtime_identity_source": _hostile("source"),
        "path": _hostile("path"),
        "auth_checked_at": "now",
        "auth_check_age_s": 1,
        "auth_failed": True,
        "auth_reason": _hostile("reason"),
        "auth_remedy": _hostile("remedy"),
    }
    before = console.width
    console.width = 1000
    # Act
    try:
        print_agent_list(None, rows=[row], verbose=True)
    finally:
        console.width = before
    rendered = capsys.readouterr().out

    # Assert
    assert all(
        value in rendered
        for value in (
            row["name"],
            row["host_display"],
            row["billing_mode"],
            row["auth_identity"],
            row["harness"],
            row["engine"],
            row["model"],
            row["stored_credential"],
            row["runtime_identity_source"],
            row["path"],
        )
    )


def test_narrow_details_are_plain_even_with_hostile_values() -> None:
    # Arrange
    row = {
        "name": _hostile("name"),
        "started_at": "-",
        "billing_mode": _hostile("billing"),
        "auth_identity": _hostile("auth"),
        "harness": _hostile("harness"),
        "engine": _hostile("engine"),
        "model": _hostile("model"),
        "stored_credential": _hostile("credential"),
        "runtime_identity_source": _hostile("source"),
        "path": _hostile("path"),
        "auth_checked_at": "now",
        "auth_check_age_s": 1,
        "auth_failed": True,
        "auth_reason": _hostile("reason"),
        "auth_remedy": _hostile("remedy"),
    }

    # Act
    rendered = "\n".join(_narrow_detail_lines(row, verbose=True))

    # Assert
    assert row["auth_reason"] in rendered and row["path"] in rendered


def test_account_table_renders_provider_account_status_identity_and_host_literally() -> None:
    # Arrange
    row = AccountRow(
        provider=_hostile("provider"),
        name=_hostile("account"),
        freshness_state="VALID",
        freshness_hours=1.0,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=datetime.now(timezone.utc).isoformat(),
        identity_state="mismatch",
        verified_email=_hostile("identity"),
        pause_reason=_hostile("status"),
        pause_since=0.0,
        host=_hostile("host"),
    )
    sink = Console(record=True, width=1000)

    # Act
    sink.print(render_stored_table([row]))
    rendered = sink.export_text()

    # Assert
    assert all(
        value in rendered
        for value in (
            row.provider,
            row.name,
            str(row.verified_email),
            row.pause_reason[:30],
            row.host,
        )
    )


def test_agent_table_strips_terminal_controls_from_all_external_values(capsys) -> None:
    # Arrange
    value = _controlled("external")
    row = {
        "name": value,
        "status": "[/]" + value,
        "started_at": "-",
        "host_display": value,
        "billing_mode": value,
        "auth_identity": value,
        "harness": value,
        "engine": value,
        "model": value,
        "stored_credential": value,
        "runtime_identity_source": value,
        "path": value,
        "auth_checked_at": "now",
        "auth_check_age_s": 1,
        "auth_failed": True,
        "auth_reason": value,
        "auth_remedy": value,
        "validation_errors": [value],
    }
    before = console.width
    console.width = 1000

    # Act
    try:
        print_agent_list(None, rows=[row], verbose=True)
    finally:
        console.width = before
    rendered = capsys.readouterr().out

    # Assert
    assert "[/]externalclickredc1" in rendered
    _assert_no_terminal_controls(rendered)


def test_dynamic_hidden_status_is_literal_and_cannot_break_markup(capsys) -> None:
    # Arrange
    row = {"name": "alpha", "status": "[/]", "started_at": "-"}

    # Act
    print_agent_list(None, rows=[row])
    rendered = capsys.readouterr().out

    # Assert
    assert "1 [/] hidden" in rendered


def test_narrow_details_strip_terminal_controls_from_external_values() -> None:
    # Arrange
    value = _controlled("narrow")
    row = {
        "name": value,
        "started_at": "-",
        "host_display": value,
        "billing_mode": value,
        "auth_identity": value,
        "harness": value,
        "engine": value,
        "model": value,
        "stored_credential": value,
        "runtime_identity_source": value,
        "path": value,
        "auth_checked_at": "now",
        "auth_check_age_s": 1,
        "auth_failed": True,
        "auth_reason": value,
        "auth_remedy": value,
    }

    # Act
    rendered = "\n".join(_narrow_detail_lines(row, verbose=True))

    # Assert
    assert "narrowclickredc1" in rendered
    _assert_no_terminal_controls(rendered)


def test_fleet_header_strips_controls_and_renders_external_values_literally() -> None:
    # Arrange
    value = "[/]" + _controlled("fleet")
    listing = FleetListing(
        reports=[
            HostReport(
                host=value,
                status=UNREACHABLE,
                instrument=value,
                detail=value,
            )
        ],
        resolutions=((value, value),),
    )
    sink = Console(record=True, width=1000)

    # Act
    print_fleet_header(sink, listing)
    rendered = sink.export_text()

    # Assert
    assert "[/]fleetclickredc1" in rendered
    _assert_no_terminal_controls(rendered)


def test_account_table_strips_terminal_controls_from_every_external_value() -> None:
    # Arrange
    value = _controlled("account")
    row = AccountRow(
        provider=value,
        name=value,
        freshness_state=value,
        freshness_hours=None,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=None,
        identity_state="mismatch",
        verified_email=value,
        pause_reason=value,
        pause_since=0.0,
        host=value,
    )
    sink = Console(record=True, width=1000)

    # Act
    sink.print(render_stored_table([row]))
    rendered = sink.export_text()

    # Assert
    assert "accountclickredc1" in rendered
    _assert_no_terminal_controls(rendered)
