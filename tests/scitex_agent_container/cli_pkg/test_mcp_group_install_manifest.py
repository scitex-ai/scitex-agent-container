"""Shared manifest normalization uses leaf-owned launch metadata only."""


def test_install_json_uses_shared_client_manifest_without_starting_server():
    # Arrange
    import json
    import sys

    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.mcp_group import mcp

    # Act
    result = CliRunner().invoke(mcp, ["install", "--json", "--dry-run"])
    payload = json.loads(result.stdout)
    declaration = payload["client_config"]["mcpServers"]["scitex-agent-container"]
    # Assert
    assert (result.exit_code, payload["runtime_verified"], declaration) == (
        0,
        False,
        {
            "command": sys.executable,
            "args": ["-m", "scitex_agent_container", "mcp", "start"],
        },
    )
