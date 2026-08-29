"""Tests for the one-row-per-account collapse of ``sac accounts list``.

No-mocks pattern: every test hand-rolls real ``AccountRow`` values and reads
the rendered table back as a string through ``rich``'s recording console, so
what is asserted is the text an operator sees, not an intermediate the
renderer might stop using.

The load-bearing case is DIVERGENCE. Collapsing is only safe for facts that
belong to the Anthropic account; credential freshness belongs to one file on
one machine, and a collapse that let three VALID hosts hide one EXPIRED one
would be a compact lie. Several tests below exist purely to hold that line.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from scitex_agent_container.cli_pkg._account_list_collapse import (
    _freshest_snapshot,
    collapse_by_account,
    render_accounts_table,
)
from scitex_agent_container.cli_pkg._account_list_render import AccountRow


def _row(name: str, host: str, **over) -> AccountRow:
    """One healthy fleet row, with any field overridable per test."""
    base = dict(
        name=name,
        host=host,
        freshness_state="VALID",
        freshness_hours=6.0,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=None,
        identity_state="verified",
        verified_email=name.replace("-scitex-ai", "@scitex.ai"),
    )
    base.update(over)
    return AccountRow(**base)


def _render(rows: list[AccountRow]) -> str:
    console = Console(record=True, width=200)
    console.print(render_accounts_table(rows))
    return console.export_text()


@pytest.fixture
def four_accounts_on_four_hosts() -> list[AccountRow]:
    """The shape the operator actually sees: 4 accounts x 4 hosts = 16 rows."""
    names = [
        "scitex-01-scitex-ai",
        "wyusuuke-gmail-com",
        "ywata1989-gmail-com",
        "ywatanabe-scitex-ai",
    ]
    hosts = ["h1", "h2", "h3", "h4"]
    return [_row(n, h) for h in hosts for n in names]


def test_sixteen_host_rows_collapse_to_four_account_groups(
    four_accounts_on_four_hosts: list[AccountRow],
) -> None:
    # Arrange — 16 rows carrying 4 distinct accounts.
    rows = four_accounts_on_four_hosts
    # Act
    groups = collapse_by_account(rows)
    # Assert
    assert len(groups) == 4


def test_collapse_preserves_first_seen_account_order() -> None:
    # Arrange
    rows = [_row("b-account", "h1"), _row("a-account", "h1"), _row("b-account", "h2")]
    # Act
    groups = collapse_by_account(rows)
    # Assert
    assert [g.name for g in groups] == ["b-account", "a-account"]


def test_group_keeps_every_hosts_reading_separable() -> None:
    """Reduce on render, not on construction — see the module docstring."""
    # Arrange
    rows = [_row("acct", "h1"), _row("acct", "h2"), _row("acct", "h3")]
    # Act
    groups = collapse_by_account(rows)
    # Assert
    assert groups[0].hosts == ["h1", "h2", "h3"]


def test_rendered_table_prints_each_account_once(
    four_accounts_on_four_hosts: list[AccountRow],
) -> None:
    # Arrange
    rows = four_accounts_on_four_hosts
    # Act
    text = _render(rows)
    # Assert
    assert text.count("wyusuuke-gmail-com") == 1


def test_hosts_cell_says_all_when_every_host_holds_the_account(
    four_accounts_on_four_hosts: list[AccountRow],
) -> None:
    # Arrange
    rows = four_accounts_on_four_hosts
    # Act
    text = _render(rows)
    # Assert
    assert "all 4" in text


def test_hosts_cell_names_the_host_missing_the_credential() -> None:
    """The host WITHOUT the credential is the actionable one.

    That is the machine whose next agent start refuses with "no healthy
    stored account", so a bare count would hide the only fact worth acting
    on.
    """
    # Arrange — present on h1/h2, absent from h3 (which holds another account).
    rows = [_row("acct", "h1"), _row("acct", "h2"), _row("other", "h3")]
    # Act
    text = _render(rows)
    # Assert
    assert "not on h3" in text


def test_status_states_the_agreed_ttl_when_hosts_agree() -> None:
    """Asserts the whole cell, not the substring ``VALID``.

    ``"VALID" in text`` also passes on the divergence rendering
    (``VALID x2; EXPIRED on h3``), so it could not tell the agree path from
    the disagree path — the one distinction this function makes.
    """
    # Arrange
    rows = [_row("acct", "h1"), _row("acct", "h2")]
    # Act
    text = _render(rows)
    # Assert
    assert "VALID +6h00m " in text


def test_status_names_the_host_whose_credential_expired() -> None:
    """A divergent credential must survive the collapse by name."""
    # Arrange — two hosts VALID, one EXPIRED.
    rows = [
        _row("acct", "h1"),
        _row("acct", "h2"),
        _row("acct", "h3", freshness_state="EXPIRED", freshness_hours=-1.0),
    ]
    # Act
    text = _render(rows)
    # Assert
    assert "EXPIRED on h3" in text


def test_status_does_not_report_a_clean_ttl_when_hosts_disagree() -> None:
    """The majority reading must not stand in for the whole group."""
    # Arrange
    rows = [
        _row("acct", "h1"),
        _row("acct", "h2"),
        _row("acct", "h3", freshness_state="EXPIRED", freshness_hours=-1.0),
    ]
    # Act
    text = _render(rows)
    # Assert
    assert "VALID x2" in text


def test_identity_says_ok_when_the_verified_email_matches_the_slug() -> None:
    """Do not restate the Account column one punctuation change apart."""
    # Arrange
    rows = [_row("scitex-01-scitex-ai", "h1", verified_email="scitex-01@scitex.ai")]
    # Act
    text = _render(rows)
    # Assert
    assert "scitex-01@scitex.ai" not in text


def test_identity_prints_the_email_when_it_does_not_match_the_slug() -> None:
    """A credential filed under the wrong label is what this column is for."""
    # Arrange
    rows = [_row("scitex-01-scitex-ai", "h1", verified_email="someone@else.com")]
    # Act
    text = _render(rows)
    # Assert
    assert "someone@else.com" in text


def test_identity_mismatch_on_one_host_wins_over_verified_on_others() -> None:
    """A wrong credential is a wrong credential, not a minority opinion."""
    # Arrange
    rows = [
        _row("acct", "h1"),
        _row("acct", "h2", identity_state="mismatch", verified_email="wrong@x.com"),
    ]
    # Act
    text = _render(rows)
    # Assert
    assert "MISMATCH" in text


def test_identity_duplicate_is_reported_over_every_other_state() -> None:
    # Arrange
    rows = [_row("acct", "h1"), _row("acct", "h2", duplicate_of="earlier-acct")]
    # Act
    text = _render(rows)
    # Assert
    assert "DUPLICATE of earlier-acct" in text


def test_identity_is_unverified_when_no_host_checked() -> None:
    # Arrange
    rows = [_row("acct", "h1", identity_state="unverified", verified_email=None)]
    # Act
    text = _render(rows)
    # Assert
    assert "unverified" in text


def test_usage_cell_takes_the_freshest_snapshot_across_hosts() -> None:
    """Usage is an Anthropic-side fact; a host only caches it."""
    # Arrange — h2 holds the newer cache.
    rows = [
        _row("acct", "h1", snapshot_as_of="2026-08-01T00:00:00+00:00"),
        _row("acct", "h2", snapshot_as_of="2026-08-17T00:00:00+00:00"),
    ]
    # Act
    groups = collapse_by_account(rows)
    # Assert
    assert _freshest_snapshot(groups[0]) == "2026-08-17T00:00:00+00:00"


def test_no_snapshot_on_any_host_yields_no_freshest_snapshot() -> None:
    """A bare ``"-" in text`` would match the table's own rule characters."""
    # Arrange
    rows = [_row("acct", "h1"), _row("acct", "h2")]
    # Act
    groups = collapse_by_account(rows)
    # Assert
    assert _freshest_snapshot(groups[0]) is None


def test_hosts_column_is_absent_on_the_single_host_path() -> None:
    """No Host anywhere means no fleet, so a Hosts column would say nothing."""
    # Arrange
    rows = [_row("acct", "")]
    # Act
    text = _render(rows)
    # Assert
    assert "Hosts" not in text
