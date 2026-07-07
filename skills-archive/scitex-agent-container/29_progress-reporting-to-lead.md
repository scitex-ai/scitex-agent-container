---
description: |
  [TOPIC] Push progress reports to the lead's a2a inbox at every milestone, so fleet coordination stops requiring polls.
  [DETAILS] sac agents send `mcp__sac__a2a_send(target='lead', ...)` push updates on PR open/merge/close, BLOCKED, DONE, or any significant context change. The lead reads its inbox to coordinate the fleet without reading every agent's `agent_logs` / `gh` state by hand. Speak/Telegram are for the OPERATOR; a2a push is for the LEAD. Both happen at milestones — not exclusive channels. Includes the KIND vocabulary, when-to / when-not-to thresholds, the one-time per-agent ACL grant the lead has to issue (`sac a2a grant --sender <name> --target lead`), and pointers to the companion Stop-hook reminder shipped via the dotfiles `_shared/to_home/` overlay.
tags: [scitex-agent-container-progress-reporting-to-lead]
---

# Progress reporting to the lead

Push a structured update to the lead's a2a inbox whenever a milestone fires. The lead polls its inbox at every Stop and uses these to coordinate the fleet without reading every agent's session.jsonl by hand.

## Tool

```python
mcp__sac__a2a_send(
    target='lead',
    content='<KIND>: <one-line summary>\n<evidence>',
    priority='normal',   # or 'high' for blockers
)
```

## When to push

Push at these events, **as soon as they happen** (don't batch):

| Event | KIND | content shape |
|---|---|---|
| PR opened | `PR_OPENED` | `repo#N branch/<name> title` |
| PR merged | `PR_MERGED` | `repo#N commit/<sha> title` |
| PR closed (not merged) | `PR_CLOSED` | `repo#N reason` |
| Task BLOCKED | `BLOCKED` | one-line reason + numbered options if applicable |
| Task DONE | `DONE` | task identifier + evidence (PR#/commit/file) |
| Major context change | `CONTEXT` | what shifted and why it matters |

## Format examples

```python
# DONE with PR
mcp__sac__a2a_send(target='lead',
    content='DONE feat/lmod-apptainer-helpers\nscitex-hpc#8 merged commit 5c9ca5e, all 20 tests pass')

# BLOCKED needing operator decision
mcp__sac__a2a_send(target='lead', priority='high',
    content='BLOCKED scitex-hpc PR #10\nbelt-and-suspenders PR redundant after #9 turned tests green; options: a) close, b) keep open as belt')

# PR opened
mcp__sac__a2a_send(target='lead',
    content='PR_OPENED scitex-agent-container#241 docs/adr-0016-provider-and-account-axes\ntitle: docs(adr): 0012 -> 0016 renumber + provider×account axes')

# Context shift
mcp__sac__a2a_send(target='lead',
    content='CONTEXT scitex-hpc tests workflow was red 4 days due to MCP-coverage gap; fixed via PR #9 zero-growth + PR #10 closed as belt-and-suspenders')
```

## Why push, not poll

The lead currently polls `agent_logs` and `gh` to track progress. That's slow:

- `agent_logs` is stale-cached (status / latest_event fields don't reflect the most recent in-turn state; the transcript is the ground truth — see memory `reference_sac_status_fields_stale_use_transcript`).
- `gh` PR API requires the lead to know which repo / PR# to check.
- Each poll burns the lead's context.

Push closes this gap. The lead's inbox is a small bounded queue; one push per milestone is essentially free.

## Don't push for

- Every Bash tool call (way too noisy).
- Every assistant text line.
- Speculative progress ("might finish in 5 min").
- Intermediate-state status that's about to change ("CI is running").

The threshold is: would a human supervisor want to know this RIGHT NOW so they could intervene or move on?

## Don't forget operator-facing channels

- **Speak (audio)** and **Telegram** are the OPERATOR's channels.
- **a2a_send** is the LEAD's channel.
- For DONE / BLOCKED / major milestones: do **both** — speak + a2a_send.

## Conflict resolution

If you already pushed a milestone earlier in this turn, you can skip the next Stop's reminder for that same milestone. A *new* milestone since the last push → push.

## ACL note

ACL-based send restrictions are not currently enforced in sac (verified 2026-05-28: no `scitex_agent_container.comms.acl` module, no `sac a2a grant` subcommand). If a future sac release adds an enforced ACL and you hit 403, file an issue against scitex-agent-container for the grant-CLI + Python API to be implemented — don't try to work around it manually.

## Companion Stop-hook reminder

A turn-end reminder hook ships via the dotfiles `_shared/to_home/` overlay at `~/.claude/hooks/stop/report_to_lead_on_stop.{sh,md}`. The shell shim emits a stderr nudge at every Stop; the `.md` is the agent-facing protocol crib pointing right back to this skill. The hook is gated on `SCITEX_AGENT_CONTAINER_NAME` so non-sac sessions (including the lead session itself) do not inherit the reminder.

## Cross-references

- `mcp__sac__a2a_inbox()` — what the lead reads (the destination of every push).
- `mcp__sac__a2a_reply(in_reply_to=..., ...)` — for follow-up replies in a conversation, NOT for fresh milestones.
- Telegram + scitex-notification audio: the OPERATOR-facing channels; a2a push complements them, doesn't replace.
- Lead doctrine `~/.claude/skills/scitex-lead/SKILL.md` — operator-facing communication rules.
- `26_credentials-rotation.md`, `27_credentials-relogin.md`, `28_credential-refresh.md` — sibling skills for the credential side of the agent lifecycle.
