---
name: agent-container-troubleshooting
description: Common launch failures and their fixes for scitex-agent-container agents.
---

# Agent Launch Troubleshooting

## "Screen session disappeared during auto-accept"

The screen session dies before the auto-accept logic can send keystrokes.

### Cause 1: `--continue` with no valid session

Claude Code's `--continue` flag fails if there's no resumable session or the deferred tool marker is stale.

**Error:** `No deferred tool marker found in the resumed session`

**Fix:** Set `session: new` in the agent YAML for first launch. Switch to `continue` after a successful session exists.

```yaml
claude:
  session: new  # Use 'new' for first launch, 'continue' after
```

### Cause 2: `mcp_channel.ts` not found

The Orochi MCP server script can't be located. The agent starts, loads the MCP config pointing to a missing file, and crashes.

**Error:** `Orochi enabled but mcp_channel.ts not found`

**Fix:** Set `spec.orochi.ts_path` in the agent YAML (preferred) or `SCITEX_OROCHI_PUSH_TS` env var.

```yaml
orochi:
  ts_path: ~/proj/scitex-orochi/ts/mcp_channel.ts
```

Resolution order for `find_mcp_channel_ts()`:
1. `spec.orochi.ts_path` from agent YAML
2. `SCITEX_OROCHI_PUSH_TS` env var
3. `import scitex_orochi` package path (only works if installed in caller's python)
4. `/opt/scitex-orochi/ts/mcp_channel.ts`

### Cause 3: Wrong python / missing packages

`scitex-agent-container` runs in the caller's python. If `scitex-orochi` isn't installed there, package-based path resolution fails silently.

**Fix:** Set `spec.venv` in the agent YAML so the screen session activates the correct virtualenv.

```yaml
spec:
  venv: ~/.venv  # Activated before claude command via source <venv>/bin/activate
```

## Debugging a failed launch

Manual launch to capture the actual error:

```bash
# 1. Kill any stale session
screen -S <name> -X quit; scitex-agent-container cleanup

# 2. Launch manually in screen with error capture
screen -dmS <name>-debug bash -l -c '
  source ~/.venv/bin/activate
  cd <workdir>
  claude --model "<model>" --dangerously-skip-permissions \
    --mcp-config <mcp-config-path> \
    --dangerously-load-development-channels server:scitex-orochi
  exec bash
'

# 3. Wait, then check screen content
sleep 10
screen -S <name>-debug -X hardcopy /tmp/<name>-debug.txt
strings /tmp/<name>-debug.txt | tail -30
```

The `strings` command handles binary characters in screen hardcopy output.

## TUI prompt handling

Claude Code shows up to 3 confirmation prompts:
1. `--dangerously-skip-permissions` → "y/n" prompt
2. `--dangerously-load-development-channels` → radio selection "1. I am using this for local development"
3. Skills trust (if new skills loaded)

The auto-accept logic in `claude_code.py` polls screen content and sends keystrokes. If it fails, the session hangs at a prompt and eventually times out.

**To send keystrokes manually:**
```bash
screen -S <name> -X stuff "1\r"  # Select option 1
screen -S <name> -X stuff "\r"   # Press Enter
```

## Host resolution (orochi.hosts)

Hosts are tried in order — first reachable wins. Place LAN IP first for speed:

```yaml
orochi:
  hosts:
    - 192.168.11.22      # LAN (fast, ~1ms)
    - scitex-orochi.com  # Internet (fallback, ~50ms)
```

The MCP config only uses `hosts[0]`. The Python sidecar (`orochi_connector.py`) tries each host in order with full logging. No silent fallback.
