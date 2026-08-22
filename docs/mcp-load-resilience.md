# MCP load-resilience — why the stdio MCP dropped under load, and the fix

Sibling of [`mcp-cold-start.md`](./mcp-cold-start.md). That doc covers the
**connect-time** race (server too slow to connect at session start). This doc
covers the **mid-session** failure: a healthy, connected stdio MCP that gets
**dropped under host load** and never comes back.

> **Scope: the Claude Code harness.** Like its sibling, this document
> describes Claude Code's stdio-MCP *client* behaviour, not sac's and not
> MCP's in general.

## The incident (2026-07-09)

The `scitex-agent-container` MCP surface (`host_exec_local`, `agent_restart`,
`agent_status`, `agent_spawn`, `db_*`, the `a2a_*` channel tools …) is exposed to
Claude Code as **stdio** MCP subprocess(es) — launched per session by the client
(`.mcp.json` → `sac mcp start` / `sac mcp channel`).

During a host load spike (load ~27 on 16 cores) the tools **vanished** from an
in-container agent and did **not** come back after load fell to ~10. A human
operator had to run `sac agents restart` by hand.

### What live dogfooding established (not process death, not a hung upstream now)

1. The MCP server **process was alive** the whole time (PID intact, ~2 h uptime).
2. Its upstream (`:7878` listen, the colocated `/v1/turn` runner) was **responsive
   again** by the time it was inspected.
3. Yet the tools were **unavailable to the client** — Claude Code had dropped the
   stdio connection and did **not** re-handshake.
4. Killing the stdio server process to force a relaunch did **not** relaunch it:
   Claude Code does not respawn a stdio MCP within a session.
5. `sac mcp healthcheck` (the boot auto-heal) could not read `claude mcp list`,
   yet **falsely reported `action=ok`** — a false-OK that hid the drop.

## Root cause

**A request handler blocked on a slow upstream long enough for the stdio client
to give up.** Under the spike the `:7878` spawn/exec path jammed. An MCP handler
that made a synchronous/awaited upstream call with an **unbounded or absurdly
long timeout** then blocked for a very long time. The stdio client (Claude Code)
timed out and **dropped the stdio MCP** — and **Claude Code does not
auto-reconnect a stdio MCP mid-session** (only HTTP/SSE transports get its
reconnect retries; see `mcp-cold-start.md`). So the tools stayed gone for the
rest of the session even though the process lived and the upstream recovered.

The two smoking-gun unbounded waits:

| call site | before | problem |
|---|---|---|
| `host_exec_local` → `request_host_exec` | `http_timeout_s = 3700.0` (~62 min) | a jammed `:7878` blocked the handler for up to an hour |
| channel `_wake_turn` → `POST /v1/turn` | `httpx.AsyncClient(timeout=None)` | a wedged runner hung the wake POST **forever** |
| channel `_consume_sse` | `httpx.AsyncClient(timeout=None)` | a hung **connect** to `:7878` blocked inside `client.stream(...)`, so the reconnect-with-backoff loop never retried |

## The fix — PREVENTION (the only viable sac-side fix)

Because a dropped **stdio** MCP can only be revived by a **full session restart**
(finding #4 — Claude Code owns the stdio child and neither respawns nor
reconnects it mid-session), there is **no sac-side post-hoc recovery**. The fix
is to **never let the connection get dropped**: bound every upstream call so no
handler can block the server long enough for the client to give up.

1. **`host_exec` client wait tracks the server contract, not a fixed 62 min.**
   `request_host_exec` now derives its HTTP timeout from the *effective*
   server-side per-command timeout (the caller's `timeout_s`, else the 300 s
   server default), clamped to `(0, 3600]`, **+ a 30 s margin** — ~330 s for the
   default instead of 3700 s. A genuinely long `sac image build` still works: pass
   `timeout_s` (up to the 3600 s server max) and the client wait tracks it.
   (`_lifecycle/_host_exec_client.py::_resolve_http_timeout`.)

2. **Wake POST is bounded.** `_wake_turn` now waits a **finite** bound
   (default 180 s = the runner's 120 s per-turn deadline + margin) instead of
   `None`. A wedged `/v1/turn` raises a `TimeoutException` that the SSE consumer's
   `on_event` wrapper catches and logs loudly, keeping the long-lived stream alive
   — never an infinite block. (`_mcp/_channel_wake.py::_resolve_wake_timeout`.)

3. **SSE connect is bounded** (30 s) while the **read stays unbounded** (the event
   stream is legitimately long-lived). A hung connect now fails fast and the
   existing backoff loop reconnects. (`_mcp/channel.py`.)

4. **Fail-fast, structured, contained.** Every bounded call surfaces a structured
   error (`host_exec_local` → `{"status":"error", …}`) or is caught by the SSE
   `on_event` wrapper — a slow/hung upstream never desyncs the stdio protocol.

5. **Honest healthcheck (no false-OK).** `sac mcp healthcheck` now returns
   `action="unknown"` (not `ok`) whenever it could not verify client-side
   connectivity (`claude mcp list` unreadable/empty, or a critical server absent
   from its output). A live server process does not prove the client is still
   connected, so a "no failures seen" reading is **never** dressed up as OK.
   (`_mcp/_healthcheck.py`.)

## Knobs

| env var | effect | default |
|---|---|---|
| `SAC_MCP_HOST_EXEC_HTTP_TIMEOUT_S` | hard override of the derived `host_exec` client HTTP wait (s) | derived from server contract (~330 s) |
| `SAC_MCP_WAKE_TIMEOUT_S` | client-side wait for the wake `POST /v1/turn` (s) | 180 |
| `SAC_MCP_SSE_CONNECT_TIMEOUT_S` | SSE **connect** timeout (read stays unbounded) (s) | 30 |
| `SAC_A2A_TURN_TIMEOUT_S` | runner's own `/v1/turn` per-turn deadline (the 504 bound the wake waits on) | 120 |

## Not in sac's control — harness recommendation

The **reconnect** half is Claude Code's domain and cannot be fixed from sac:

- **A dropped stdio MCP is only revived by a full session restart.** Claude Code
  does not respawn a stdio child nor reconnect it mid-session. sac's honest
  healthcheck can *report* the loss, but the boot-time `--fresh` self-restart it
  brokers is a full session restart — there is no in-session revival.
- **The durable "auto-reconnect without a restart" fix is an HTTP/SSE transport.**
  Claude Code *does* reconnect HTTP/SSE MCP servers (its 3-initial / 5-mid-session
  retries). `sac mcp start --http --port <p>` already serves the tool surface over
  HTTP; pointing `.mcp.json` at a long-lived, supervised HTTP MCP endpoint (rather
  than a per-session stdio child) would let the client transparently reconnect
  after a load-induced drop — no session restart, no vanished tools. That requires
  a supervised long-lived MCP endpoint + bearer auth on the tool port (the tools
  include `host_exec`), so it is a larger, separate change; this PR lands the
  prevention layer that keeps the *stdio* transport from dropping in the first
  place.
