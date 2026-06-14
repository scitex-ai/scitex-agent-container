# Periodic-drive turn consumer (agent-side)

When you (the agent) receive an a2a inbox event with
`kind == "periodic_drive"`, treat its `body` as a SYSTEM RE-INJECTION
of your standing rules, current mission, and current work state.
This is NOT a new task from the lead/operator — it's the sac
periodic-drive lane keeping you on-mission across context churn.

The envelope:

```json
{
  "kind": "periodic_drive",
  "body": "[sac periodic drive — <name>]\n\n## Standing rules\n…\n## Current mission\n…\n## Current work (state.db + git)\n…\n## Action\n…",
  "generated_at": <unix_ts>,
  "from_agent": "sac-periodic-drive"
}
```

## What to do on receipt

1. **Re-read your state** as described in the body. The fields are
   pulled mechanically from your `spec.yaml` + the registry's
   active git worktree — they are the AUTHORITATIVE current view.

2. **Decide your next action** toward your mission, using the
   re-injected rules. Common patterns:
   - Mid-task → CONTINUE the task; the drive is a heartbeat, not
     a context switch.
   - Waiting on Spartan/CI/lead → CHECK status, report if changed,
     otherwise stay idle.
   - Idle without pending work → re-read your mission, propose
     the next concrete action to lead via a2a.
   - Mission complete → SIGNAL lead via a2a; do not start a new
     mission unilaterally.

3. **Do NOT echo the body back**. The drive lane is a one-way
   reminder; replying to `sac-periodic-drive` is a no-op (the
   sender is a daemon, not an agent).

## What NOT to do

- Do not treat the drive turn as a new directive from the lead.
- Do not start a new mission you weren't already on.
- Do not flood the lead with status reports — only when the state
  changed.
- Do not modify code in response to the drive turn unless the
  body's `Action` section tells you to.

## Opt-out

The lane is opt-in by default (`spec.periodic_drive.enabled: true`).
To disable per-agent: `spec.periodic_drive.enabled: false`.
Operator can pause the whole fleet via
`SAC_PERIODIC_DRIVE_DISABLED=1` on the listen process env.

## See also

- `_lifecycle/_periodic_drive.py` — sweep + envelope builder.
- `_lifecycle/_periodic_drive_loop.py` — listen-server lifespan
  ticker.
- Lead a2a: `4973264a`, `12a0d8f6`, `7916f486`.
