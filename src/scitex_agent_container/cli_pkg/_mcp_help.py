"""Shared help specifications for targeted MCP discovery and observation."""

from scitex_dev.ecosystem import CliHelp, Example

CONNECTION_HELP = CliHelp(
    summary="Validate critical Claude Code MCP connections.",
    description=(
        "Runs `claude mcp list` without requesting a restart. This observes "
        "Claude Code, not every vendor's MCP client.",
    ),
    examples=(
        Example("{prog} mcp validate-connections"),
        Example("{prog} mcp validate-connections --json"),
    ),
    exit_codes={
        0: "Verified connected status for all critical MCP servers.",
        1: "Unknown, disabled or failed connections.",
        2: "Invalid command usage.",
    },
    version_of="scitex-agent-container",
)

TOOL_DESCRIPTION_HELP = CliHelp(
    summary="Describe one registered MCP tool without running its operation.",
    description=(
        "Match NAME exactly. JSON contains name, description and inputSchema. "
        "A registered schema does not establish tool readiness.",
    ),
    examples=(Example("{prog} mcp show-tool agent_send --json"),),
    exit_codes={
        0: "Tool description printed.",
        1: "MCP dependencies or schema description unavailable.",
        2: "Unknown tool name or invalid command usage.",
    },
    version_of="scitex-agent-container",
)
