"""Only an identified MCP status row can establish connectivity."""

import pytest

from scitex_agent_container._mcp._healthcheck import parse_mcp_status


@pytest.mark.parametrize(
    "text",
    [
        "other: runner --name scitex-cards - Connected",
        "scitex-cards-extra: runner - Connected",
        "prefix-scitex-cards: runner - Connected",
        "diagnostic mentions scitex-cards - Connected",
        "scitex-todo-extra: runner - Connected",
        "scitex-cards: runner - Disconnected",
        "scitex-cards: runner - Connecting",
        "scitex-cards: runner --connected",
        "scitex-cards: runner --ready - Unknown",
        "scitex-cards: runner - Not ready",
    ],
)
def test_ambiguous_row_does_not_establish_connectivity(text):
    # Arrange
    servers = ("scitex-cards",)

    # Act
    result = parse_mcp_status(text, servers)

    # Assert
    assert result == {"scitex-cards": "unknown"}


@pytest.mark.parametrize("name", ["scitex-cards", "scitex-todo"])
@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Connected", "connected"),
        ("✓ Connected", "connected"),
        ("Ready", "connected"),
        ("Failed to connect", "failed"),
        ("✗ Failed to connect", "failed"),
        ("Not connected", "failed"),
    ],
)
def test_identified_status_preserves_canonical_and_legacy_names(name, marker, expected):
    # Arrange
    text = f"{name}: runner --error-log errors.log - {marker}"

    # Act
    result = parse_mcp_status(text, ("scitex-cards",))

    # Assert
    assert result == {"scitex-cards": expected}
