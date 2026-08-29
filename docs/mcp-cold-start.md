# MCP cold-start connect race — root cause + fix

> **Scope: the Claude Code harness.** Everything below is a property of
> Claude Code's stdio-MCP *client*, not of sac or of MCP generally. An
> agent on another harness has a different MCP client and does not
> inherit these behaviours.

## The incident (2026-07-06)

Agents' `scitex-agent-container` (`sac mcp start`) and `scitex-todo`
(`scitex-todo mcp start`) **stdio** MCP servers intermittently fail to connect at
Claude Code session start — per-agent and racy (some agents connect, others do
not). The servers start healthy and write nothing to stdout; the failure is a
**client-side cold-start connect-timing race**:

- The heavy `fastmcp` import makes the stdio server slow to become ready
  (~4.4 s of `import fastmcp` on a cold / slow container FS — see profiling
  below), intermittently exceeding Claude Code's MCP startup/connect timeout.
- Claude Code does **NOT** auto-reconnect a failed **stdio** MCP (its
  3-initial / 5-mid-session retries are HTTP/SSE only). So a lost race leaves
  the agent missing `host_exec` / `agent_spawn` / `db_*` / the todo tools for
  the **entire session**.

### Profiling (coverage-free, `perf_counter`)

| phase | ms |
|---|---|
| `import fastmcp` | ~2100 |
| `from fastmcp import FastMCP` | ~2300 |
| import all 9 sac tool modules (eager) | ~40 |
| `FastMCP()` construct | ~6 |
| `register_all_tools` schema-gen (~35 tools) | ~300 |

`import fastmcp` dominates; the tool modules are ~40 ms.

## The fix

1. **`alwaysLoad: true`** on the critical stdio servers in `.mcp.json` — forces
   Claude Code to **block** on startup until the server connects (capped at
   `MCP_TIMEOUT`) instead of the default racy background connect. Real field
   (v2.1.121+; a harmless no-op on older clients). Stamped on defensively at
   materialize time by `runtimes/_mcp_reliability.inject_always_load`
   (`_to_home_deployers._deploy_mcp_merge`, `mcp_config._setup_mcp_from_servers`);
   the `.mcp.json` deep-merge preserves it.
2. **`MCP_TIMEOUT=30000`** (ms) in the agent launch env — the client MCP
   **startup** timeout (distinct from the per-server `timeout` field, which is a
   per-tool-call cap). Injected by `runtimes/_apptainer_listen_env.listen_env_flags`.
3. **Lazy-import** of the tool modules (`_mcp/server.py` imports them inside
   `_build_server()`, not at module load) so `import scitex_agent_container._mcp`
   stays cheap.
4. **Auto-heal boot self-check** — `sac mcp healthcheck` (`_mcp/_healthcheck.py`),
   run at boot via a `SessionStart` hook: logs the expected capability surface,
   detects failed critical MCPs via `claude mcp list`, and on failure alarms +
   requests a rate-limited `--fresh` self-restart. FAIL-OPEN — never blocks boot.

## Knobs

| env var | effect |
|---|---|
| `MCP_TIMEOUT` | client MCP startup connect timeout (ms) — set to `30000` |
| `SAC_MCP_HEALTHCHECK_DISABLED` | `1`/`true` disables the boot self-check |
| `SAC_MCP_HEALTHCHECK_NO_RESTART` | `1`/`true` → alarm only, never self-restart |
| `SAC_MCP_HEALTHCHECK_STATE_DIR` | override the restart-cooldown sentinel dir |

## Not fixed here

The ecosystem-wide `fastmcp` version pin is scitex-dev's domain. sac's
`pyproject.toml` carries an unpinned `fastmcp>=2.0`; `sac versions --json --live
--base-only` reports the fastmcp version across SIFs.

## See also

[`mcp-load-resilience.md`](./mcp-load-resilience.md) — the sibling **mid-session**
failure: a connected stdio MCP that gets **dropped under host load** (a handler
blocking on a jammed upstream → the client times out and drops the stdio server,
which it never reconnects). The fix there is prevention — bounded upstream
timeouts on every handler — plus the honest-UNKNOWN healthcheck referenced above.
