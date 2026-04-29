---
name: agent-container-auto-accept
description: Modular TUI prompt detection and auto-acceptance for Claude Code.
---

# Auto-Accept TUI Prompts

Claude Code shows confirmation prompts for dangerous flags. The auto-accept system handles them via modular handlers in `runtimes/prompts.py`.

## Built-in Handlers

Ordered by priority (lowest number runs first). See
`src/scitex_agent_container/runtimes/prompts.py::PROMPT_HANDLERS` for
the canonical list.

| Priority | Name | Detects | Sends |
|---:|------|---------|-------|
| 1 | bypass-permissions   | "2. Yes, I accept" + "Bypass Permissions"         | `2`, `Enter` |
| 2 | dev-channels         | "1. I am using this for local development"        | `1`, `Enter` |
| 3 | thinking-effort      | "1. Medium (recommended)" + "thinking"            | `1`, `Enter` |
| 4 | mcp-json-edit        | "1. Yes, proceed" in the `.mcp.json` edit dialog  | `1`, `Enter` |
| 5 | skip-permissions-yn  | Legacy y/n skip-permissions / trust prompt        | `y`, `Enter` |
| 6 | press-enter-continue | Informational banner / context-window warning     | `Enter` |
| 7 | file-trust           | "Do you trust the files in this folder?" (y/n)    | `y`, `Enter` |
| 8 | file-trust-radio     | "1. Yes, I trust this folder" (radio variant)     | `1`, `Enter` |
| 9 | theme-selection      | "1. Auto (match terminal)" on first run           | `1`, `Enter` |
| 10 | login-method        | "2. Anthropic Console account · API usage billing"| `2`, `Enter` |

## Design

- **Number keys** sent directly (not arrow keys) — cursor-position independent
- **Order-agnostic** — all handlers checked each poll cycle
- **Detects by option text** (e.g., "2. Yes, I accept") for reliability
- Uses `capture-pane` (tmux) or `hardcopy` (screen) to read content

## Adding New Handlers

```python
from scitex_agent_container.runtimes.prompts import register_prompt, PromptHandler

register_prompt(PromptHandler(
    name="my-new-prompt",
    detect=lambda c: "3. My Option" in c and "Enter to confirm" in c,
    keys=["3", "Enter"],
    priority=4,
))
```

## Runtime Prompt Detection (via mamba-healer)

In addition to startup auto-accept, mamba-healer's health scan includes runtime permission prompt detection. This catches prompts that appear mid-session (e.g., new tool permissions, MCP reconnect confirmations).

**Detection patterns** (checked every health scan cycle):
- "Do you want to" — general permission prompt
- "Allow X to" — tool/MCP permission
- "Enter to confirm" — confirmation dialog

**Excluded** (false positive prevention):
- Status bar text like "bypass permissions on" — not an actual prompt

If a runtime prompt is detected, healer reports `stuck_prompt` status and can auto-respond via tmux `stuff`. This complements the startup auto-accept handlers above.

## Disabling Auto-Accept

```yaml
spec:
  claude:
    auto_accept: false   # Manual TUI acceptance required
```

## Diagnostics

Logged to `~/.scitex/agent-container/logs/{name}/auto-accept.log`:
- Every poll: pane content snapshot, elapsed time
- Handler matches with timestamps
- Timeout diagnostics with last captured content
