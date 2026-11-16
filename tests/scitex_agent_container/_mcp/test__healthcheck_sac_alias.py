"""The health check recognizes the same SAC key as startup reliability."""

import pytest

from scitex_agent_container._mcp._healthcheck import parse_mcp_status
from scitex_agent_container.runtimes._mcp_reliability import CRITICAL_MCP_SERVERS


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("Connected", "connected"), ("Failed to connect", "failed")],
)
def test_startup_sac_alias_reports_canonical_health(marker, expected):
    # Arrange
    text = f"sac: sac mcp start - {marker}"
    server = "scitex-agent-container"

    # Act
    result = parse_mcp_status(text, (server,))

    # Assert
    assert ("sac" in CRITICAL_MCP_SERVERS, result) == (True, {server: expected})


@pytest.mark.parametrize("name", ["sac-backup", "other-sac", "another"])
def test_sac_alias_does_not_match_other_server_rows(name):
    # Arrange
    text = f"{name}: sac mcp start - Connected"

    # Act
    result = parse_mcp_status(text, ("scitex-agent-container",))

    # Assert
    assert result == {"scitex-agent-container": "unknown"}


def test_explicit_sac_name_keeps_requested_result_key():
    # Arrange
    text = "sac: sac mcp start - Connected"

    # Act
    result = parse_mcp_status(text, ("sac",))

    # Assert
    assert result == {"sac": "connected"}
