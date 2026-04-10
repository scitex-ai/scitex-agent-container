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

### Cause 2: Wrong python / missing packages

If the claude command depends on tools that live in a specific virtualenv
(e.g. scitex CLIs), the screen session needs to activate that venv before
running claude.

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
  claude --model "<model>" --dangerously-skip-permissions
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

