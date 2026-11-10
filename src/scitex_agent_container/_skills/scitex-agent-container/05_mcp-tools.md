---
description: |
  [TOPIC] SAC MCP server, discovery and client configuration
  [DETAILS] Start the bundled stdio or HTTP server, discover actual tool names
  and input schemas, and load package skills on demand. CLI and MCP surfaces
  share domain implementations but do not expose identical capabilities/options.
tags: [scitex-agent-container-mcp-tools]
---

# SAC MCP server

SAC bundles an MCP server; install its optional dependency first:

```bash
pip install 'scitex-agent-container[mcp]'
sac mcp doctor
sac mcp list-tools --json
```

`doctor` checks the local MCP installation and registration. It does not prove
that a client connected or that a particular agent operation will succeed.
`list-tools --json` returns names/descriptions, not input schemas. An actual MCP
client obtains schemas through `tools/list` after connecting.

## Client configuration

For a client supporting this configuration format:

```json
{
  "mcpServers": {
    "scitex-agent-container": {
      "command": "sac",
      "args": ["mcp", "start"]
    }
  }
}
```

Ensure `sac` resolves in the client's environment, or supply its absolute path.
`sac mcp install --claude-code` prints a configuration excerpt; it does not
install dependencies or edit a client configuration file.

```bash
sac mcp start --dry-run
sac mcp start                         # stdio, normally launched by the client
sac mcp start --http --port 8970      # HTTP; loopback bind by default
```

HTTP startup validates `--port` in the range 0–65535 before preview or backend
loading. Stdio ignores this HTTP-only option.

`mcp channel` is a separate A2A channel adapter, not this server's entry point.
`mcp healthcheck` includes alarms and possible self-restart requests; it is not
a read-only replacement for discovery.

## Discover capabilities before calling

Standalone tools use bare names such as `agent_list`, `agent_send`,
`account_show`, `store_clean` and `skills_get`. Do not prepend `sac_` or infer
names from CLI verbs. CLI and MCP options and return shapes can differ; for
example, CLI logs are reached through `agents tail`, while MCP retains
`agent_logs`. An aggregator may mount the server under its own namespace;
inspect that server's actual advertised tools rather than assuming a prefix.

Use `skills_list` to discover packaged skill names. Then call `skills_get`
with a listed stem, for example `{"name": "02_quick-start"}`. Only listed files
resolving within the package's skill root are admitted. Load the relevant
skill and tool description when needed instead of injecting every guide into
agent context.

Some wrappers call the CLI, while others use direct Python or host-authority
paths. Registration is explicit; adding a CLI command does not automatically
expose it as an MCP tool. Lifecycle, messaging, storage, image and host-execution
tools may change state. Obtain the caller's authorization before invoking them
and respect each tool's own backend admission checks. Discovery alone proves
neither authorization nor readiness, delivery or completion.

Source: `_mcp/server.py`, `_mcp/_tools/__init__.py`, `_mcp/_tools/_skills.py` and
`cli_pkg/mcp_group.py`. Behavioral controls:
`tests/scitex_agent_container/_mcp/test_server.py`,
`tests/scitex_agent_container/_mcp/_tools/test___init__.py`,
`tests/scitex_agent_container/_mcp/_tools/test__skills.py` and
`tests/scitex_agent_container/cli_pkg/test_mcp_group.py`. These do not establish
external vendor configuration or a deployed server's availability.

HTTP argument controls: `tests/scitex_agent_container/cli_pkg/test__port_options.py`.
