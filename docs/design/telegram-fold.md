# Telegram fold into sac MCP — design

Status: **Phase 2 + Phase 3 complete** — bridge ported from orochi, allowed-
users filter enforced, flock singleton with stale-PID recovery in place, six
transport tools backed by the in-process bridge, default-on registration
(`SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=0` to opt out). Phase 1 scaffolding
landed at commit `c877354` on develop; Phase 2+3 in
`feature/telegram-fold-phase2`.
Audit reference: `/home/ywatanabe/proj/lead/GITIGNORED/dev/05_sac-mcp-telegram.md`.

## Launcher dependency (READ THIS FIRST)

The inbound side relies on the Claude Code launcher being invoked with
`--dangerously-load-development-channels server:scitex-agent-container`.
Without that flag, Claude silently drops every `notifications/claude/channel`
emission the bridge produces. The bridge logs a WARN at startup
(`telegram: bridge starting. NOTE: inbound channel notifications require the
Claude Code launcher to be invoked with …`) so a misconfigured launcher is
loud — but Claude will not surface the failure on its own.

The lead repo's launcher script owns that flag; a separate subagent ships
the launcher change.

## How to enable

1. Set `LEAD_TELEGRAM_AUTH_TOKEN` in the lead's `~/.scitex/lead/.env`. Any
   strong random string works (the launcher mints one if absent).
2. Set `SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN` (or
   `SAC_TELEGRAM_BOT_TOKEN`) in the same env to the Telegram BotFather token.
3. Add an agent spec with `spec.telegram.allowed_users: ["<your-tg-user-id>"]`.
4. Make sure the launcher passes
   `--dangerously-load-development-channels server:scitex-agent-container`.
5. Start the lead's Claude session. The bridge boots inside the
   `scitex-agent-container` MCP server (the FastMCP process Claude spawns),
   acquires the per-token flock, and begins long-polling.

## How to disable

* Per-session: set `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=0` in the lead's
  env. The tools de-register; the bridge still boots but its outbound
  surface is unreachable from Claude.
* Per-spec: set `spec.telegram.auto_connect: false`. The bridge does not
  start; outbound tools return `{"error": "telegram bridge is not
  initialised on this host"}`.
* Globally: unset `LEAD_TELEGRAM_AUTH_TOKEN`. The startup hook short-
  circuits and the bridge is never constructed.

## Phase 2 + Phase 3 — what shipped

* **TelegramBridge** (`src/scitex_agent_container/_telegram/_bridge.py`):
  aiohttp-backed long-poll, allowed-users filter, channel-notifier
  injection point, lock-acquire-before-IO startup, graceful shutdown.
* **TelegramBridgeLock** (`_telegram/_lock.py`): per-bot-token `flock` at
  `~/.scitex/agent-container/runtime/telegram/<token-hash>.lock`; reclaims
  the lock when the recorded PID is dead (`kill(pid, 0)` → `ESRCH`).
* **Startup hook** (`_telegram/_startup.py`): `maybe_start_bridge(spec, …)`
  returns a constructed bridge when `LEAD_TELEGRAM_AUTH_TOKEN` +
  bot-token env are present and `spec.telegram.auto_connect=true`; logs
  the launcher-flag WARN; registers the instance in the runtime
  singleton so the MCP tools can find it.
* **Runtime singleton** (`_telegram/_runtime.py`): `set_bridge` /
  `get_bridge` / `clear_bridge` + the bridge's auth token shared with
  the tools layer.
* **MCP tools** (`_mcp/_tools/_telegram.py`): six transport tools, each
  gated by the auth token (caller's `LEAD_TELEGRAM_AUTH_TOKEN` env must
  match the bridge's). Subagents inherit a sanitised env, so they get a
  structured `{"error": "telegram tools are lead-only; ..."}` response
  instead of touching the bridge.
* **MCP server boot** (`_mcp/server.py`): `_build_server()` now calls
  `_maybe_boot_telegram_bridge(server)` after `register_all_tools`.
  Bridge runs in the same process as the FastMCP server, which is the
  process spawned by Claude Code when it loads the lead's
  `scitex-agent-container` MCP server.
* **Feature-flag flip**: `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD` now
  defaults to ON. Set to `0` / `false` / `off` / `no` to opt out.

## Goal

Fold claude-code-telegrammer's transport-layer tools into sac's MCP surface
and reuse sac's per-agent SSE inbox bus for inbound Telegram traffic, so that
the lead and fleet agents speak one MCP language for both A2A and Telegram
messaging, and the standalone telegrammer MCP can be retired for sac fleet
usage.

## Architecture

```
                                   +---------------------+
                                   |  Telegram Bot API   |
                                   +----------+----------+
                                              ^
                                              | sendMessage / sendDocument
                                              | getUpdates (long-poll)
                                              v
                                   +----------+----------+
                                   |   TelegramBridge    |
                                   | (sac listen child;  |
                                   |  bot-token singleton|
                                   |  lock file)         |
                                   +----+-----------+----+
                inbound                 |           |        outbound
              source="telegram"         |           |  (from any sac MCP
                                        v           ^   client invoking
                              +---------+---------+ |   telegram_* tools)
                              | sac A2A inbox bus | |
                              | (Broker per agent)| |
                              | _inbox_bus.py     | |
                              +----+-----+-----+--+ |
                                   |     |     |    |
                              SSE  |     |     | SSE|
                                   v     v     v    |
                              +----+--+--+--+--+--+ |
                              | sac mcp channel   |-+
                              | (stdio sidecar in |
                              |  every agent's    |
                              |  Claude session)  |
                              +-------------------+
                                       ^
                                       |  notifications/claude/channel
                                       v
                              +-------------------+
                              | Agent Claude      |
                              | (lead, master,    |
                              |  workers...)      |
                              +-------------------+
```

Inbound flow: Telegram update -> `TelegramBridge._process_update` ->
`POST /agents/<target>/message:send` with `metadata.source="telegram"` ->
target agent's `Broker` -> SSE -> `sac mcp channel` -> Claude session sees
`<channel source="telegram" chat_id=... message_id=...>`.

Outbound flow: agent's Claude session calls `telegram_send` / `telegram_reply`
(MCP tool exposed by sac admin MCP) -> sac process invokes
`TelegramBridge.send_message` (in-process for the local case; or a thin HTTP
hop to `sac listen` for the cross-process case in Phase 3+) -> Telegram API.

## Tool surface (final sac MCP, post Phase 3)

| tool | signature | does | side-effect |
| --- | --- | --- | --- |
| `telegram_send` | `(chat_id: str, text: str, reply_to: int \| None = None) -> dict` | Send a new message or reply. Direct outbound from any agent. | write (network -> Telegram API) |
| `telegram_reply` | `(chat_id: str, text: str, row_id: int \| None = None, reply_to: int \| None = None, mark_read: bool = True) -> dict` | Telegrammer-shaped reply: threading + optional mark-read of an inbound row. | write |
| `telegram_react` | `(chat_id: str, message_id: int, emoji: str) -> dict` | Emoji reaction on a TG message. | write |
| `telegram_edit_message` | `(chat_id: str, message_id: int, text: str) -> dict` | Edit a prior bot message. | write |
| `telegram_download_attachment` | `(file_id: str, dest_dir: str \| None = None) -> dict` | Resolve Telegram file_id, download bytes, return local path. | read (network) / write (fs) |
| `telegram_send_document` | `(chat_id: str, path: str, caption: str \| None = None) -> dict` | Upload local file as a TG document. | write (fs read + network) |

Open: whether `telegram_send` and `telegram_reply` should collapse to one
tool. Audit suggests keeping both — telegrammer's `reply` carries the
`row_id` + `mark_read` semantics that don't apply to a fresh outbound send.
For Phase 1 we scaffold them as separate stubs and let Phase 3 decide.

## What we drop / sidecar

The 5 persistence tools (`get_history`, `get_unread`, `mark_read`,
`search_messages`, `get_context`) are **not** ported into sac. From the audit
(`05_sac-mcp-telegram.md`):

> The split is clean: transport tools (reply/react/edit/download/send_document)
> belong wherever the bot token lives; persistence tools
> (history/unread/mark_read/search/get_context) are telegrammer's distinct
> value-add and only make sense if you keep the SQLite store.

Sac's bus is intentionally non-persistent (`_inbox_bus.py` comments: "No
persistence: replay belongs on disk (`session.jsonl`), not in the bus.").
Re-introducing a SQLite store into sac MCP would contradict that.

If history/search must survive, ship a separate `tg-archiver` sidecar that
subscribes to the bus and writes telegrammer-compatible SQLite. Out of scope
for this fold.

## Allowed-users model

`TelegramSpec.allowed_users` (already in
`config/_parsers/_telegram.py`) is the only access list. The bridge enforces
it at two points:

1. Inbound: in `_process_update`, drop any update where
   `update.message.from.id` is not in `allowed_users` (when the list is
   non-empty; empty list = no Telegram inbound, fail closed).
2. Outbound: tools accept any `chat_id`, but the bridge logs a warning when
   the target chat does not match a known allowed user. The bot can only
   reach chats it has membership in anyway — Telegram enforces this server-
   side.

`bot_token_env` (default `SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN`) names
the env var holding the bot token. The bridge never accepts tokens via
config files.

## Lifecycle

- **Connect**: at `sac listen` startup if any agent spec sets
  `spec.telegram.auto_connect=True` and the env token resolves. Otherwise
  on-demand (Phase 4: `telegram_connect` admin tool, out of scope here).
- **Disconnect**: at `sac listen` shutdown — `TelegramBridge.stop()`
  cancels the poll task and closes the aiohttp session.
- **Singleton enforcement**: bridge takes an exclusive flock on
  `${state_dir}/telegram-bridge.lock` (state_dir = sac's per-host state
  root). If the lock is held by another live PID, the bridge refuses to
  start and logs the holder. **Stale-lock protection**: on lock-acquire we
  read the PID from the file; if `kill(pid, 0)` raises `ProcessLookupError`,
  the lock is stale — we delete it and retry once. This is the failure mode
  that the standalone telegrammer suffers from (lock file from a crashed
  process blocks future starts until manual `rm`); calling it out so Phase 2
  ports the orochi `deleteWebhook`+offset-flush dance *plus* the stale-lock
  recovery.
- **Bot-token uniqueness**: only one process per bot token may long-poll
  Telegram (409 Conflict otherwise). The lock file plus orochi-style
  `deleteWebhook` + `getUpdates(offset=-1)` flush at startup guarantees a
  clean slot.
- **Health check**: `getMe` at startup populates `self._bot_name`; failure
  aborts the start. Phase 4 may add a periodic `getMe` heartbeat.

## Migration plan

- **Phase 1 (this PR)**: design doc + scaffolding (empty `_telegram/`
  package, `TelegramBridge` skeleton raising `NotImplementedError`, MCP
  tool stubs raising `NotImplementedError`, feature-flagged off via
  `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=1`, tests locking the import
  surface). No behaviour change for sac users.
- **Phase 2**: port `TelegramBridge` from
  `/home/ywatanabe/proj/scitex-orochi/src/scitex_orochi/_telegram_bridge.py`
  to sit on the sac broker instead of the orochi channel. Add the lock-file
  singleton with stale-PID recovery. Wire startup hook into `sac listen`.
- **Phase 3**: implement the 6 transport tools (`telegram_send`,
  `telegram_reply`, `telegram_react`, `telegram_edit_message`,
  `telegram_send_document`, `telegram_download_attachment`). Flip the
  feature flag default to on once green.
- **Phase 4**: end-to-end test — real Telegram round-trip through a sac
  agent's MCP. Add metrics / heartbeat.
- **Phase 5**: docs, examples, CHANGELOG entry; coordinate retirement of
  `~/proj/claude-code-telegrammer` for sac-fleet usage and removal of
  `_telegram_bridge.py` from orochi (separate PR).

## Open questions for the user

- Migration confirmation: orochi still owns the bot token in code today
  (`_telegram_bridge.py` unchanged in the last 3 days). Confirm the
  transition window — when does orochi stop polling so sac can start?
- Inbound routing target: orochi posted to `#telegram`. Sac's broker is
  per-agent; default target should probably be `master`. Configurable?
  Multi-subscriber fanout?
- Persistence: do you want a `tg-archiver` sidecar that re-creates
  telegrammer's SQLite store from the bus, or is `session.jsonl` enough?
- Cross-host: today the bus is per-`sac listen`. Should Telegram-driven
  messages reach agents on other hosts in this round, or wait for the
  generic cross-host A2A story?
- `telegram_send` vs `telegram_reply`: collapse into one tool with optional
  `row_id`/`reply_to`, or keep both shapes?
