---
description: |
  [TOPIC] PaneActions
  [DETAILS] Typed, logged vocabulary for pane-mediated agent operations — each PaneAction subclass implements snapshot/precheck/send/is_complete and run_action records every attempt to the host-wide SQLite store at ~/.scitex/agen....
tags: [scitex-agent-container-pane-actions, pane-actions]
---

# PaneActions

Typed, logged vocabulary for pane-mediated operations on a running
agent (probing liveness, compacting context, sending a key sequence).

## Architecture

Each action subclasses `PaneAction` and implements four hooks:

| Hook | Purpose |
|---|---|
| `snapshot` | Capture pre-state from the multiplexer pane. |
| `precheck` | Decide whether the action is allowed (e.g. agent must be idle). |
| `send` | Drive the action via `send_keys` / multiplexer commands. |
| `is_complete` | Poll the pane to detect completion. |

`run_action(action, agent_name)` orchestrates the four hooks and
classifies every attempt into exactly one outcome:

- `success` — `is_complete` returned True within the timeout.
- `precondition_fail` — `precheck` rejected the attempt.
- `send_error` — `send` raised or returned an error.
- `completion_timeout` — `is_complete` never returned True.
- `skipped_by_policy` — caller-side policy (e.g. quota) blocked the run.

Every attempt is appended as one row to the host-wide SQLite store at
`~/.scitex/agent-container/runtime/actions.db` for offline analysis.

## Built-ins

- `NonceProbeAction` — functional liveness check (sends a unique nonce,
  verifies it appears in pane output).
- `CompactAction` — context-window compaction with drop-verification.

## CLI surface

```bash
sac actions run nonce-probe <agent-name>
sac actions query --agent <name> --outcome success
sac actions stats --agent <name>
sac actions purge --days 30 --yes
```

See `sac actions --help` for full flag list.
