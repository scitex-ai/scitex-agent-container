---
description: |
  [TOPIC] sac MCP server — exposed tools, transport, install snippet
  [DETAILS] Convention §3-§5 surface: every `sac mcp <verb>` plus the
  bare-name MCP tools (`agent_*`, `db_*`, `host_*`, `image_*`,
  `template_*`, `account_*`, `quota_*`, `skills_*`, `mcp_*`,
  `list_python_apis`). Both stdio and HTTP transports. Install with
  `pip install scitex-agent-container[mcp]`.
tags: [scitex-agent-container-mcp-tools]
---

# sac MCP server

Implements convention §13 (CLI ↔ MCP parity). Every `sac <noun> <verb>`
on the CLI is reachable as an MCP tool with the same parameter shape.

## Install

```bash
pip install scitex-agent-container[mcp]   # adds fastmcp
sac mcp doctor                             # verify
```

## Subcommands (CLI face)

```bash
sac mcp start                          # stdio (default)
sac mcp start --http --port 8970       # HTTP transport
sac mcp doctor                         # version + tool count + registration
sac mcp list-tools [--json]            # enumerate tools
sac mcp install [--claude-code]        # config snippet
```

## Claude Code config

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

`sac mcp install --claude-code` prints the same snippet for copy-paste.

## Tool inventory

Bare-name `<verb>_<noun>` shape per Convention A. The sac umbrella mount
adds the `agent_container_` prefix at scitex aggregator time.

| Group       | Tools                                                                                                                                                                                                                                  |
|-------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `agent`     | `list`, `status`, `logs`, `health`, `find`, `check`, `validate`, `inspect`, `recall`, `check_priority`, `take_snapshot`, `start`, `stop`, `restart`, `attach`                                                                          |
| `db`        | `show`, `query`, `clean`, `tick`, `migrate`, `export`, `import`                                                                                                                                                                        |
| `host`      | `list`, `validate`, `probe`, `exec`                                                                                                                                                                                            |
| `image`     | `build`                                                                                                                                                                                                                                |
| `template`  | `render_contributor_spec`                                                                                                                                                                                                              |
| `account`   | `account_show`, `quota_watch`                                                                                                                                                                                                          |
| `skills`    | `skills_list`, `skills_get` — convention §5 mandatory pair                                                                                                                                                                             |
| `info`      | `list_python_apis`, `mcp_list_tools`, `mcp_doctor` — self-introspection                                                                                                                                                                |

Get the live list any time with `sac mcp list-tools` (or, from Python,
`scitex_agent_container._mcp.server.get_server().list_tools()`).

## Implementation pointers

- `src/scitex_agent_container/_mcp/server.py` — `FastMCP` instance,
  lazy-built so the bare `import` doesn't require fastmcp.
- `src/scitex_agent_container/_mcp/_tools/` — one file per noun group.
  Each tool wraps the Click CLI through `_helpers.invoke_cli_*` so
  CLI ↔ MCP parity stays automatic as new commands land.
- `src/scitex_agent_container/cli_pkg/mcp_group.py` — Click face
  (`start / doctor / list-tools / install`).
- `src/scitex_agent_container/_mcp_server.py` — re-export shim so the
  scitex-dev `audit-mcp-tools` linter's hard-coded path works.

## Mutating verbs

`agent_start`, `agent_stop`, `agent_restart`, `db_clean`, `db_export`,
`db_import` mutate state. The MCP server itself does not gate them —
the host (Claude Code, custom embedder) is expected to mediate via its
own permission flow before invoking the tool.
