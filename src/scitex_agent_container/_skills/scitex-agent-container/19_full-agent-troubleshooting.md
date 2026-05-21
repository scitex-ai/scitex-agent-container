---
description: |
  [TOPIC] Full-agent (sac peer) troubleshooting — operational recipes
  [DETAILS] Companion to 18_full-agent-delegation.md. Operational deep-dives for running sac peer fleets in practice: observing peers via Monitor (push notifications over polling), hard/soft skill mode internals, the reaper pattern for fleets that don't self-exit, stuck-peer failure modes (idle SDK turns, registry-vs-reality drift, empty `--mission` results), and a consolidated common-pitfalls table. Use when 18_*.md got you started and you now need to diagnose a real peer that misbehaved or scale out a fleet.
tags: [scitex-agent-container-full-agent-troubleshooting, full-agent, claude-session, sac, fleet, debugging]
---

# Full-agent (sac peer) troubleshooting

This leaf is the operational companion to
[18_full-agent-delegation.md](18_full-agent-delegation.md). 18 covers
the *contract* (when to delegate, YAML, lifecycle); this one covers
*what goes wrong in practice* and the patterns that fix it.

## Observing peers — Monitor over polling

Don't poll `heartbeat.json` in a tight loop. Use the parent's
`Monitor` tool with `tail -F` on each peer's `session.jsonl` to get
push notifications the moment a peer emits an assistant line, says
`DONE`/`BLOCKER`, or ends a turn:

```bash
PEER_RUNTIME=$HOME/.scitex/agent-container/runtime
for p in <peer1> <peer2> <peer3>; do
  tail -F $PEER_RUNTIME/$p/session.jsonl 2>/dev/null \
    | python3 -u -c "
import json, sys
for L in sys.stdin:
    try: d=json.loads(L)
    except: continue
    t=d.get('type','?'); text=d.get('text','') or ''
    if t=='assistant' and text.strip():
        if 'DONE' in text or 'BLOCKER' in text:
            print(f'[$p] *** ' + text.strip()[:300], flush=True)
        else:
            print(f'[$p] said: ' + text.strip()[:200], flush=True)
    elif t=='result':
        print(f'[$p] turn ended', flush=True)
" &
done
wait
```

Wrap this whole block in a `Monitor` invocation. Each notification
arrives in chat as it happens, no fixed-interval `ScheduleWakeup`
needed.

Why this matters: when the orchestrator is Claude Code CLI, peers
cannot push state back to it (no `/v1/turn` endpoint on the CLI side —
see "A2A asymmetry" in 18). Filesystem `tail -F` is the supported
push channel.

## Hard / soft skill modes

`spec.skills` has two modes — pick `required` (hard) or `available`
(soft) per skill:

| Mode | Spec field | What materialises into CLAUDE.md | When to use |
|---|---|---|---|
| **hard** | `spec.skills.required: [foo]` | `@<absolute-path>` line so the SDK inlines content at session start | invariants the agent must apply (procedure, validity gate, security rule) |
| **soft** | `spec.skills.available: [foo]` | name + path under `## Available Skills` (no `@-import`) | references / examples / optional patterns |

Caveat (resolved 2026-05-05): `runtime: claude-session` previously
ignored both modes. F-CS1 (commit `2483f6f` on
`feat/f-cs1-claude-session-skills`) extends `claude-session` to honour
both uniformly via the existing `setup_claude_md` /
`build_skills_lines` helpers. Until merged to develop, include the
absolute path to any required skill in the mission text as a safety
net.

## Reaper pattern (when delegating multiple agents)

If the parent launches a *fleet* of peer agents (one per capsule, per
PR, per dataset), runners do not auto-exit when their mission ends — the
SDK loop keeps polling for new instructions. To free slots:

1. Have each agent write a sentinel file (`workdir/data/results/score.json`)
   on completion.
2. Run a watchdog (`sleep 30 && pgrep`) that kills runners whose sentinel
   indicates done. See `paper-scitex-clew/GITIGNORED/FAILED_PATTERNS.md`
   §6 for the failure mode and the schema-tolerant detection logic.

## Stuck-peer failure modes

The SDK loop is forgiving — it almost never crashes loudly. Instead,
peers go quiet in a small handful of recognisable ways:

| Symptom | Likely cause | First-line fix |
|---|---|---|
| Peer goes idle mid-task (no new assistant text, heartbeat still fresh) | model alias under-provisioned, or context drift | `sac peer post-turn <name> "Continue from where you left off..."`; if that fails, escalate the same yaml to `model: opus` (or `claude-opus-4-7[1m]`) |
| Empty `result` after `--mission` (no assistant text, no tool calls) | model alias not resolving or prompt rejected silently | stop, restart with a shorter mission and `model: opus` |
| `sac agents status` says `stopped` but `heartbeat.json` is fresh | sac registry-vs-reality drift | trust heartbeat over registry; `sac agents restart` only if heartbeat is also stale |
| Long startup_command timing out | the SDK does ONE turn per `--mission` then idles | drive subsequent turns with `sac peer post-turn` rather than packing everything into the mission |
| `nohup bash` loses login PATH → `sac` not found on the remote | login env not sourced | `bash -lc 'setsid nohup …'` or hard-code `~/.env-3.11/bin/sac` |

Once you know which row you're in, the parent's `Monitor` view of
`session.jsonl` will usually confirm — a "[result] turn ended" with no
preceding "[assistant]" line is the signature of the empty-`--mission`
mode.

## Common pitfalls

| Pitfall | Fix |
|---|---|
| Heavy data under `~` on Spartan → quota | put workdirs under `/tmp` or project FS (`/data/gpfs/projects/<punim>/...`) |
| Re-launching an already-passed agent | parent's done-detection must accept all score-schema variants |
| Subagent-style narrow prompt | include "you are a *full* agent — use Bash, run tests, commit" explicitly |
| Forgetting the exit signal | always require the agent to print `DONE <name> …` on stdout — gives the parent a clean handle |
| Passing oracle path "for the verifier only" to an agent peer | honor-system masking — agent runs as same user, mere knowledge of path enables anchoring. Make the verifier a separate peer with separate identity; don't pass the path. |

## Related

- [18_full-agent-delegation.md](18_full-agent-delegation.md) — the contract this leaf troubleshoots
- [15_claude-session.md](15_claude-session.md) — runtime details
- [07_a2a-protocol.md](07_a2a-protocol.md) — agent-to-agent message bus
- [40_troubleshooting.md](40_troubleshooting.md) — package-wide troubleshooting (multiplexer, auto-accept, etc.)
