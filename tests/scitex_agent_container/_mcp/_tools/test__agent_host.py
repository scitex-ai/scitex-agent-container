"""Real MCP start requests defer selected-source/auth checks to the host."""

from __future__ import annotations

import pytest

from scitex_agent_container._mcp._tools._agent import agent_start
from tests.scitex_agent_container.cli_pkg.test__send_host import host_endpoint


@pytest.mark.parametrize(
    "mode, expected",
    (
        (None, None),
        ("continue", "continue"),
        ("fresh", "fresh"),
        ("resume", "resume"),
        ("new-session", "fresh"),
    ),
)
def test_mcp_host_start_preserves_explicit_session_policy(
    env_save_restore, mode, expected
):
    # Arrange
    with host_endpoint(env_save_restore, 200, {"returncode": 0}) as calls:
        # Act
        agent_start("retained-owner", session=mode)
    # Assert
    assert calls[0][1] == (
        {"name": "retained-owner", "assume_yes": True}
        | ({"session": expected} if expected else {})
    )


def test_mcp_start_queued_is_not_running_admission(env_save_restore):
    # Arrange
    with host_endpoint(
        env_save_restore,
        202,
        {"status": "accepted", "poll": "/agents/retained-owner/status"},
    ) as calls:
        # Act
        result = agent_start("retained-owner", session="continue")
    # Assert
    assert (result["status"], result["admission"], len(calls)) == (
        "pending",
        "unproven",
        1,
    )


@pytest.mark.parametrize("status", (401, 403, 404, 409, 502))
def test_mcp_start_host_denial_preserves_authority(env_save_restore, status):
    # Arrange
    with host_endpoint(env_save_restore, status, {"error": "host refused"}) as calls:
        # Act
        result = agent_start("retained-owner")
    # Assert
    assert (result["status"], result["http_status"], len(calls)) == ("error", status, 1)


def test_mcp_start_invalid_session_never_posts(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 200, {"returncode": 0}) as calls:
        # Act
        result = agent_start("retained-owner", session="typo")
    # Assert
    assert (result["status"], calls) == ("error", [])


def test_mcp_start_keeps_foreground_and_consent(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 200, {"returncode": 0}) as calls:
        # Act
        agent_start("retained-owner", foreground=True)
    # Assert
    assert calls[0][1] == {
        "name": "retained-owner",
        "foreground": True,
        "assume_yes": True,
    }


@pytest.mark.parametrize("returncode", (None, False, "0", 1))
def test_mcp_start_without_a_valid_cli_exit_cannot_claim_success(
    env_save_restore, returncode
):
    # Arrange
    with host_endpoint(env_save_restore, 200, {"returncode": returncode}):
        # Act
        result = agent_start("retained-owner", session="continue")
    # Assert
    assert result["status"] == "error"


def test_mcp_accepted_start_keeps_pending_even_with_a_zero_exit_hint(env_save_restore):
    # Arrange
    with host_endpoint(env_save_restore, 202, {"status": "accepted", "returncode": 0}):
        # Act
        result = agent_start("retained-owner", session="continue")
    # Assert
    assert (result["status"], result["admission"]) == ("pending", "unproven")


@pytest.mark.parametrize("mode", ("continue", "resume", "fresh"))
def test_old_host_cannot_silently_drop_explicit_continuity(env_save_restore, mode):
    # Arrange — equal package versions cannot establish protocol support.
    with host_endpoint(
        env_save_restore,
        200,
        {"returncode": 0},
        health_response={"ok": True, "service": "sac-listen", "version": "0.29.4"},
    ) as calls:
        # Act
        result = agent_start("retained-owner", session=mode)
    # Assert
    assert (result["status"], calls, calls.health_reads) == (
        "error",
        [],
        [("/v1/health", "Bearer synthetic-host")],
    )


def test_default_start_keeps_old_host_spec_without_capability_guess(env_save_restore):
    # Arrange
    with host_endpoint(
        env_save_restore, 200, {"returncode": 0}, health_response={"ok": True}
    ) as calls:
        # Act
        result = agent_start("retained-owner")
    # Assert
    assert (result["status"], len(calls), calls.health_reads) == ("ok", 1, [])


def test_explicit_continuity_probes_the_authenticated_route_before_post(
    env_save_restore,
):
    # Arrange
    with host_endpoint(env_save_restore, 200, {"returncode": 0}) as calls:
        # Act
        agent_start("retained-owner", session="continue")
    # Assert
    assert (calls.health_reads, len(calls)) == (
        [("/v1/health", "Bearer synthetic-host")],
        1,
    )


@pytest.mark.parametrize(
    "capability",
    (
        False,
        {"protocol": "unknown", "modes": ["continue"]},
        {"protocol": "sac.start-session/v1", "modes": []},
    ),
)
def test_unknown_host_session_capability_refuses_before_post(
    env_save_restore, capability
):
    # Arrange
    response = {"service": "sac-listen", "capabilities": {"start_session": capability}}
    with host_endpoint(
        env_save_restore, 200, {"returncode": 0}, health_response=response
    ) as calls:
        # Act
        result = agent_start("retained-owner", session="continue")
    # Assert
    assert (result["status"], calls) == ("error", [])
