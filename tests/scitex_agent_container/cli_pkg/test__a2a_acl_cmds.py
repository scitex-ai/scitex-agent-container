"""Real grant-store controls for revoke planning and explicit approval."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.state_store_grants import (
    grant_send,
    has_grant,
    list_comms_grants,
)
from scitex_agent_container.cli_pkg.a2a_group import a2a


@pytest.fixture
def existing_grants(pg_schema):
    """All records are in the existing fixture's private PostgreSQL schema."""
    grant_send(sender="alpha", target="beta", note="chosen direction")
    grant_send(sender="beta", target="alpha", note="reverse direction")
    grant_send(sender="gamma", target="delta", note="unrelated pair")
    return list_comms_grants()


@pytest.mark.parametrize("approval", [[], ["--yes"], ["-y"]])
def test_revoke_preview_preserves_grants_with_or_without_approval(
    existing_grants, approval
):
    # Arrange
    arguments = ["revoke", "alpha", "beta", "--dry-run", *approval]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (result.exit_code, "would revoke" in result.stdout, list_comms_grants()) == (
        0,
        True,
        existing_grants,
    )


def test_revoke_without_approval_preserves_the_live_grant(existing_grants):
    # Arrange
    arguments = ["revoke", "alpha", "beta"]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (result.exit_code, "--yes" in result.stderr, list_comms_grants()) == (
        1,
        True,
        existing_grants,
    )


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_approved_revoke_withdraws_only_the_chosen_direction(existing_grants, approval):
    # Arrange
    arguments = ["revoke", "alpha", "beta", approval]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (
        result.exit_code,
        has_grant(sender="alpha", target="beta"),
        has_grant(sender="beta", target="alpha"),
        has_grant(sender="gamma", target="delta"),
    ) == (0, False, True, True)


@pytest.mark.parametrize("approval", [[], ["--yes"]])
def test_revoke_preview_does_not_require_a_reachable_store(approval):
    # Arrange: the unconditional store guard points to unreachable port 1.
    arguments = ["revoke", "alpha", "beta", "--dry-run", *approval]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (result.exit_code, "existing grant not checked" in result.stdout) == (
        0,
        True,
    )


def test_unapproved_revoke_refuses_before_opening_the_store():
    # Arrange: no PostgreSQL fixture or configured database is available.
    arguments = ["revoke", "alpha", "beta"]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (result.exit_code, "without --yes" in result.stderr) == (1, True)


@pytest.mark.parametrize("name", ["list-grants", "grants"])
def test_grant_listing_json_and_legacy_warning_use_separate_streams(
    existing_grants, name
):
    # Arrange
    arguments = [name, "--json"]
    # Act
    result = CliRunner().invoke(a2a, arguments)
    # Assert
    assert (
        result.exit_code,
        json.loads(result.stdout),
        "list-grants" in result.stderr,
    ) == (0, existing_grants, name == "grants")
