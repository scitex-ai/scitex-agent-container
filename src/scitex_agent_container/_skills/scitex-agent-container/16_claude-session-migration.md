---
name: claude-session-migration
description: Step-by-step recipe for migrating an existing claude-code agent (CLI runtime, tmux) to claude-session (SDK runtime, no tmux). Includes a parity-check phase, a controlled cutover with the original kept alongside as fallback, observability commands, and a tested rollback. Use when flipping a fleet agent from the legacy CLI runtime to the SDK runtime.
tags: [scitex-agent-container, claude-session, migration, fleet]
---

# Migrating an existing agent to `runtime: claude-session`

Single YAML key edit, but the safe flow has four phases: parity-check
→ side-by-side → cutover → soak. See
[15_claude-session.md](15_claude-session.md) for the runtime itself.

## When NOT to migrate

- Agent depends on **interactive** typing into tmux (`/clear`,
  `/compact`, paste). SDK takes one mission prompt at start.
- MCP servers started by hand inside tmux (not in `spec.mcp_servers`).
  The SDK only wires what's in the YAML.
- Heavy load, can't tolerate a 10-second cutover gap.

## Phase 1 — parity check (no production touch)

Prove the SDK can produce something sensible before flipping anything.

1. Copy the existing YAML to a sandbox name and switch the runtime:

   ```bash
   AGENT=head-ywata-note-win
   SANDBOX=${AGENT}-sdk
   cp ~/.scitex/orochi/shared/agents/$AGENT/$AGENT.yaml /tmp/$SANDBOX.yaml
   sed -i "s/runtime: claude-code/runtime: claude-session/" /tmp/$SANDBOX.yaml
   sed -i "s/^metadata:/metadata:\n  labels:\n    sandbox-of: $AGENT/" /tmp/$SANDBOX.yaml
   # Make sure the workdir doesn't collide with the live agent.
   sed -i "s|workdir:.*|workdir: /tmp/$SANDBOX-workspace|" /tmp/$SANDBOX.yaml
   ```

2. Run **one SDK turn** in foreground and inspect the response:

   ```bash
   sac start /tmp/$SANDBOX.yaml --foreground
   ```

   Pass criteria:
   - The runner prints assistant text to stdout (not just an `[error]`
     line).
   - The closing `[result]` reports a non-zero `output_tokens` and a
     `session_id`.
   - If the original agent uses MCP, ask the SDK turn to invoke one
     of those tools — the assistant should call it (visible in
     `~/.scitex/agent-container/runtime/$SANDBOX/session.jsonl` as
     a `pretool` event in `event_log`).

3. If parity fails, capture the transcript and abort:

   ```bash
   cat ~/.scitex/agent-container/runtime/$SANDBOX/session.jsonl
   sac stop $SANDBOX  # if daemon-mode left anything around
   ```

   Common causes: missing MCP server in YAML, tool the agent expects
   isn't actually available without the tmux session env, or the
   mission relies on bash heredoc state the SDK doesn't have.

## Phase 2 — side-by-side soak (8–24 h)

Goal: run the SDK version *alongside* the live one and compare.

1. Don't replace the live YAML. Drop the sandbox YAML into the
   discovery path under a `-sdk` suffix:

   ```bash
   mkdir -p ~/.scitex/orochi/shared/agents/$SANDBOX
   cp /tmp/$SANDBOX.yaml ~/.scitex/orochi/shared/agents/$SANDBOX/$SANDBOX.yaml
   sac start $SANDBOX
   ```

2. Compare quota over a typical workload window (run both for the
   same wall-clock period against the same kind of prompts, then):

   ```bash
   echo "=== legacy (claude-code) ==="
   sac show-status $AGENT --json | jq '{quota_5h_used_pct, quota_7d_used_pct}'
   echo "=== SDK (claude-session) ==="
   sac show-status $SANDBOX --json | jq '.sdk_session.quota'
   ```

   The SDK side reports cumulative tokens; the legacy side reports
   percentage of plan. They're not directly comparable, but a 10x
   difference in either direction is a red flag.

3. Compare event_log activity:

   ```bash
   diff <(sac show-status $AGENT --json | jq .event_log) \
        <(sac show-status $SANDBOX --json | jq .event_log)
   ```

   Hooks should fire at roughly the same rate — same tool-use pattern,
   same prompt frequency.

4. If anything diverges meaningfully, stop the sandbox and flag for
   investigation. Don't proceed to cutover.

## Phase 3 — controlled cutover

Goal: flip the live YAML, keep the legacy one stoppable as fallback.

1. Snapshot the live agent's session id so you can resume into the
   SDK side:

   ```bash
   LIVE_SID=$(sac show-status $AGENT --json | jq -r .session_id)
   echo "live session id: $LIVE_SID"
   ```

   (For `claude-session` agents the SDK runner already persists the
   id to `state_dir/session_id` and the next `sac start` auto-resumes
   — but we don't have that for the legacy CLI runtime. The session
   carries over via Claude Code's own
   `~/.claude/projects/<encoded>/<uuid>.jsonl` file, which the SDK
   resume= path also reads.)

2. Stop the live agent, edit its YAML in place, start it back up:

   ```bash
   sac stop $AGENT
   sed -i.bak "s/runtime: claude-code/runtime: claude-session/" \
       ~/.scitex/orochi/shared/agents/$AGENT/$AGENT.yaml
   # If you captured a session id and want to seed it (otherwise
   # sac auto-discovers from state_dir/session_id on the next run):
   if [ -n "$LIVE_SID" ]; then
       mkdir -p ~/.scitex/agent-container/runtime/$AGENT
       echo "$LIVE_SID" > ~/.scitex/agent-container/runtime/$AGENT/session_id
   fi
   sac start $AGENT
   ```

3. Verify the runtime swap landed and the agent answered the mission:

   ```bash
   sac show-status $AGENT --json | jq '.runtime, .sdk_session.heartbeat.state'
   sac show-logs $AGENT
   ```

4. Stop the parallel sandbox now that the live agent is on SDK:

   ```bash
   sac stop $SANDBOX
   rm -rf ~/.scitex/orochi/shared/agents/$SANDBOX
   ```

## Phase 4 — soak window (one release cycle)

Don't drop the legacy backup yet. Keep `~/.scitex/orochi/shared/agents/$AGENT/$AGENT.yaml.bak`
in place for at least one minor-version cycle. Watch:

- `sac show-status $AGENT --json` heartbeat state stays in
  `idle / working` (not stuck in `starting` or `stopping`).
- `event_log.summarize($AGENT)` still produces sensible counts.
- Quota burn rate (`sdk_session.quota.turns`) tracks workload
  reasonably — not 10x the pre-migration rate.

## Rollback

```bash
sac stop $AGENT
mv ~/.scitex/orochi/shared/agents/$AGENT/$AGENT.yaml.bak \
   ~/.scitex/orochi/shared/agents/$AGENT/$AGENT.yaml
sac start $AGENT
sac show-status $AGENT --json | jq .runtime  # back to claude-code
```

The SDK runtime never deletes the legacy CLI's
`~/.claude/projects/<encoded>/*.jsonl` files, so resume continuity
across rollback works the other way too.

## Order of fleet rollout

Per-agent — no flag day. Migrate in escalating-blast-radius order;
soak each one for at least one release cycle before the next:

1. `dev-helper` (local, no fleet duties)
2. `head-ywata-note-win` (local head; SSH fallback)
3. `telegrammer-ywata-note-win` (single inbound channel)
4. `head-mba` / `head-nas` (remote, one at a time)
5. `head-spartan` (SLURM-tenant; most moving parts — last)

## Audit checklist (run before declaring "migrated")

- `.runtime == "claude-session"` and `sdk_session.heartbeat.state` in
  `{idle, working}`
- `sdk_session.quota.turns` increments over a 1 h window
- `event_log.summarize($AGENT).hook_event_counts` shows
  `pretool / posttool / prompt / stop` (Python hooks bridged)
- `sac show-logs $AGENT` renders recent assistant turns
- `$AGENT.yaml.bak` still in place for rollback
- `restart`, `health`, `a2a`, `orochi` blocks carried over verbatim
