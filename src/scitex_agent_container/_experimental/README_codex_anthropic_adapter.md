# Codex app-server → Anthropic Messages adapter (MVP / POC)

**Status: experimental spike.** Not imported by the package, not wired into any
spec or CLI. Lives under `_experimental/` on purpose. See the module docstring
in `codex_anthropic_adapter.py` for the authoritative detail — this note is the
one-screen summary.

## What it proves

The **engine-swap** path works end-to-end: a client that only speaks the
Anthropic Messages API (the Claude Code box) can be driven by the **OpenAI
Codex** engine (gpt-5.5) with **zero client changes** — just
`ANTHROPIC_BASE_URL=http://127.0.0.1:8787`.

```
Claude Code / any Anthropic client
   │  POST /v1/messages          (Anthropic Messages shape)
   ▼
codex_anthropic_adapter.py       (stdlib http.server, no new deps)
   │  JSON-RPC 2.0 over stdio
   ▼
`codex app-server`               (OAuth auto-read from ~/.codex/auth.json)
   ▼
OpenAI Codex (gpt-5.5)
```

Auth reuses the operator's existing `~/.codex` login — no API key, no re-auth.
The codex driver is a faithful reproduction of the proven probe
`~/.scitex/agent-container/runtime/tmp/codex_appserver_test.py`.

## Protocol (codex app-server)

`initialize` → `initialized` → `thread/start{model}` (response `.result.thread.id`)
→ `turn/start{threadId, input:[{type:text,text}]}` → reply streams as
`item/agentMessage/delta` notifications, then `item/completed`
(`item.type==agentMessage`, `.text` = full reply), then `turn/completed`.

## Run

```bash
python -m scitex_agent_container._experimental.codex_anthropic_adapter serve
python -m scitex_agent_container._experimental.codex_anthropic_adapter smoke  # in-process round-trip
```

## MVP simplifications (deliberate)

- Non-streaming, text-only (`stream:true` ignored).
- `system` + `messages[]` flattened into one role-tagged text prompt; every
  request = a fresh codex thread (no 1:1 history mapping).
- Thread-per-request (spawn + tear down codex per call; no reuse).
- `usage` token counts estimated (~4 chars/token), not codex-reported.
- No tool_use / tool_result bridging (rendered as text).
- No endpoint auth / rate-limit / error mapping (loopback-only, generic 5xx).

## Production follow-ups (NOT in this MVP)

1. SSE streaming — emit the Anthropic event sequence from codex deltas.
2. tool_use / tool_result bridging ↔ codex tool/exec items.
3. Warm app-server + per-conversation thread reuse (latency/auth amortisation).
4. Real usage accounting from codex `turn/completed`.
5. Error + rate-limit + auth-expired mapping to Anthropic HTTP status/`error.type`.
6. Endpoint auth (honour `x-api-key` / `authorization`).
7. Coordinate with scitex-genai / litellm routing instead of a bespoke server;
   multi-account `~/.codex` rotation (mirroring sac account rotation).

## Verified

Live round-trip captured (this branch): a `POST /v1/messages` with
`{"model":"gpt-5.5", messages:[{role:user, content:"Reply with exactly one word: pong"}]}`
returned a valid Anthropic response `content[0].text == "pong"` in ~2s, driven by
`codex app-server` reading the operator's `~/.codex` OAuth. HTTP 200.
