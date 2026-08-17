"""``sac accounts list`` refreshes the quota snapshot before rendering it.

Operator ruling 2026-08-17, asked twice: "sac accounts list should
automatically refresh the snapshot beforehand ... it is just time consuming
for me to type it by myself." The first answer was a 5-minute systemd timer
(PR #1085), which refreshes the host that RUNS the timer and leaves every
other host rendering yesterday's numbers.

WHY A STALE SNAPSHOT IS WORSE THAN NO SNAPSHOT. Measured the same day, same
account, two hosts:

    ywata-note-win cache (1d old)    scitex-01-scitex-ai   7d = 15%
    scitex-compute-03, refreshed     scitex-01-scitex-ai   7d = 100%

The stale reading says the account has headroom while it is capped until
Aug 22. This is the command an operator reaches for to decide which account
to use, so an inverted number here is not cosmetic. It also had a live
consequence the same night: compute-03's cache was 23h old with every
percentage None, the account picker's rule is "keep the pinned account
unless its quota is KNOWN-bad", None is not known-bad, and scitex-hub was
therefore kept on a 7d-100% account and could not take a single turn.

The refresh is USAGE-ONLY and never touches a credential — a credential
refresh rotates a single-use token and would revoke every other host
holding it.
"""

from __future__ import annotations

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_list_cmd import account_list


def _help() -> str:
    return CliRunner().invoke(account_list, ["--help"]).output


def test_the_opt_out_flag_is_documented():
    """An operator who needs a fast offline read must be able to find it."""
    # Arrange
    text = _help()
    # Act
    present = "--no-refresh-quota" in text
    # Assert
    assert present is True


def test_refresh_quota_defaults_to_on():
    """The whole point: no flag typed, snapshot still fresh.

    Asserted on the parameter's real default rather than on prose, so
    flipping the default silently cannot leave this green.
    """
    # Arrange
    params = {p.name: p for p in account_list.params}
    # Act
    default = params["refresh_quota"].default
    # Assert
    assert default is True


def test_the_opt_out_flag_turns_it_off():
    """`--no-refresh-quota` must actually reach False, not merely parse."""
    # Arrange
    params = {p.name: p for p in account_list.params}
    # Act
    flag_value = params["refresh_quota"].flag_value
    # Assert
    assert flag_value is False


def test_help_states_that_no_credential_is_touched():
    """The safety property must be findable at the point of use.

    A reader deciding whether this command is safe during an incident should
    not have to read the source to learn it refetches usage only.

    Whitespace is COLLAPSED before matching. Click rewraps option help to the
    terminal width, so "USAGE ONLY" is split across a line break and a naive
    substring check fails against text that plainly contains the phrase —
    the first version of this test did exactly that. Asserting on rendered
    help means asserting on a layout you do not control.
    """
    # Arrange
    text = " ".join(_help().lower().split())
    # Act
    says_usage_only = "usage only" in text
    # Assert
    assert says_usage_only is True
