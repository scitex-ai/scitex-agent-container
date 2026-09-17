"""Every externally-derived Rich cell is literal text, never markup."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console

from scitex_agent_container.cli_pkg._account_list_render import (
    AccountRow,
    render_stored_table,
)
from scitex_agent_container.cli_pkg._helpers._agent_list import print_agent_list
from scitex_agent_container.cli_pkg._helpers._agent_list_render import (
    _narrow_detail_lines,
)
from scitex_agent_container.cli_pkg._helpers._console import console


def _hostile(field: str) -> str:
    return f"[{field}[link=https://evil.invalid]click[/link]"


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
