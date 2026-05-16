---
description: |
  [TOPIC] Telegram fold — sac MCP transport tools + channel-push inbound.
  [DETAILS] Phase 2+3 wiring: TelegramBridge runs in the sac MCP server,
  long-polls Telegram, emits notifications/claude/channel on inbound,
  and backs the six telegram_* MCP tools on outbound. Covers the
  launcher dependency on --dangerously-load-development-channels, the
  per-bot-token flock with stale-PID recovery, and the lead-only auth
  gate via LEAD_TELEGRAM_AUTH_TOKEN.
tags: [scitex-agent-container-telegram-integration]
---

# Telegram integration (Phase 2 + 3)

The `_telegram/` package folds claude-code-telegrammer's transport surface
into sac MCP. The lead's Claude session owns the bot token and emits
`notifications/claude/channel` for inbound messages; subagents reach
Telegram via the `telegram_*` MCP tools through the same in-process bridge.

## When this skill is relevant

* You need to send a Telegram message from a sac agent.
* You're debugging why inbound Telegram messages aren't reaching Claude.
* You're audit-checking the per-bot-token lock or the auth-token gate.

## Quick check — is the bridge running?

```python
from scitex_agent_container._telegram import get_bridge

bridge = get_bridge()
print(bridge is None)        # True on subagents; False on the lead
print(bridge.is_running)     # True after start(), False before
print(bridge.allowed_users)  # ['123456']
```

## Inbound flow (Telegram → Claude)

1. Bridge long-polls Telegram with `getUpdates`.
2. Each update is filtered through `TelegramSpec.allowed_users` — an
   empty list fails closed (nobody allowed). The filter compares
   `update.message.from.id` against the list.
3. Allowed updates become a `{"content": <text>, "meta": {...}}` payload
   on the bridge's `notifier` callable. `_mcp/server.py` wires that
   callable to the MCP session, which emits a
   `notifications/claude/channel` push.
4. Claude renders `<channel source="telegram" chat_id=... message_id=...
   user_id=... username=...>` to the running session.

**Launcher dependency**: Claude Code only delivers
`notifications/claude/channel` pushes when the launcher is invoked with
`--dangerously-load-development-channels server:scitex-agent-container`.
Without it the notification is silently dropped. The bridge logs a WARN
at startup to make the dependency visible; check the MCP server's stderr.

## Outbound flow (Claude → Telegram)

The six MCP tools all share the same shape:

* `telegram_send(chat_id, text, reply_to=None)`
* `telegram_reply(chat_id, text, row_id=None, reply_to=None, mark_read=True)`
* `telegram_react(chat_id, message_id, emoji)`
* `telegram_edit_message(chat_id, message_id, text)`
* `telegram_download_attachment(file_id, dest_dir=None)`
* `telegram_send_document(chat_id, path, caption=None)`

Each tool checks the caller's `LEAD_TELEGRAM_AUTH_TOKEN` against the
bridge's stored token. Subagents inherit a sanitised env without the
token, so they get `{"error": "telegram tools are lead-only; ..."}` and
the bridge is never touched.

## Feature flag

Default ON in Phase 3 (was opt-in in Phase 1). Set
`SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=0` to disable tool registration on
this server.

## Singleton lock

The bridge takes an exclusive `flock` at
`~/.scitex/agent-container/runtime/telegram/<token-hash>.lock` (note:
`runtime/`, not `containers/`). Stale-PID recovery: if the recorded PID
is dead (`kill(pid, 0)` → `ESRCH`), the lock is reclaimed automatically.
This is the failure mode the standalone telegrammer suffers from
(crashed PID leaves a dangling file; future starts block until manual
`rm`).

If you ever see `TelegramLockError: telegram bridge lock ... is held by
another live process`, run `ps -p <pid>` on the recorded PID to confirm
the holder. The bridge will not start a second poller against the same
bot token — Telegram returns 409 Conflict on dual `getUpdates`.

## Don't touch

* `~/proj/claude-code-telegrammer/` — being retired for sac-fleet use.
* The Phase 1 stubs — replaced; the import surface is unchanged.
