---
description: |
  [TOPIC] sac MCP server — how it relates to the CLI, and the gotchas
  [DETAILS] CLI↔MCP parity (every `sac <noun> <verb>` is an MCP tool of the same shape), install extra, Claude Code config, which verbs mutate state, and where the impl lives. The live tool list is self-describing — call `sac mcp list-tools` (or read the MCP schema); this leaf does NOT re-table it.
tags: [scitex-agent-container-mcp-tools]
---

# sac MCP server — the parity contract

Implements convention §13 (CLI ↔ MCP parity): **every `sac <noun>
<verb>` on the CLI is reachable as an MCP tool with the same parameter
shape** (bare `<verb>_<noun>` name per Convention A; the scitex
aggregator adds the `agent_container_` prefix at mount time).

Because of that parity, the tool inventory and every tool's parameters
are self-describing. Get the live list — never a stale hand-copied
table:

```bash
sac mcp list-tools [--json]     # or, from the host, read the MCP tool schema
```

## Install + wire into Claude Code

```bash
pip install scitex-agent-container[mcp]   # adds fastmcp
sac mcp doctor                             # verify registration
sac mcp install --claude-code             # prints the mcpServers snippet
```

The Claude Code config just runs `sac mcp start` (stdio; `--http
--port N` for HTTP transport) as the server command — `sac mcp
install` emits the exact snippet, so there's nothing to memorize here.

## Gotchas

- **Mutating verbs are NOT gated by the server.** `agent_start`,
  `agent_stop`, `agent_restart`, `db_clean`, `db_export`, `db_import`
  change state, and the MCP server invokes them unconditionally — the
  host (Claude Code / your embedder) is expected to mediate via its own
  permission flow before the call.
- **No MCP server is bundled with agents.** sac agents spawn their own
  via `to_home/.mcp.json`; `sac mcp start` is the server you point a
  host at, not something the runner auto-launches.

## Where it lives (read the code for the how)

- `_mcp/server.py` — `FastMCP` instance, lazy-built so a bare `import`
  doesn't require fastmcp.
- `_mcp/_tools/` — one file per noun group; each tool wraps the Click
  CLI through `_helpers.invoke_cli_*`, which is *why* parity stays
  automatic as new commands land.
- `cli_pkg/mcp_group.py` — the `sac mcp` Click face.
- `_mcp_server.py` — re-export shim for the scitex-dev
  `audit-mcp-tools` linter's hard-coded path.
