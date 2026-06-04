---
description: |
  [TOPIC] Stay responsive to the operator by doing every heavy job in the background.
  [DETAILS] Default doctrine for every sac agent: never block the main conversation turn on long-running work (LaTeX compile, figure builds, pytest, training, big git ops, image builds, large data downloads). Launch heavy work as a BACKGROUND process — `Bash run_in_background=true` or `Agent run_in_background=true` — end the current turn promptly, and handle the result when the runtime delivers the completion notification. The operator's Telegram message is delivered to the same inbox the conversation reads; while a turn is in flight, new Telegram messages QUEUE and are not picked up until the turn ends. The cure is a short turn, not a more clever interrupt. Short turns also keep the prompt cache warm (5-min TTL) and let other peers/the lead drive the agent without waiting minutes per round-trip.
tags: [scitex-agent-container-responsiveness, background-work, telegram-latency, short-turns]
---

# Responsiveness — short turns + background heavy work

> Operator UX rule (lead directive, 2026-06-04): the operator's
> Telegram message must be answered within seconds, never minutes.

> **Structural enforcement**: a pre-tool-use hook
> ``~/.claude/hooks/pre-tool-use/force_background_bash.sh`` (shipped
> under ``examples/agents/full-agent/to_home/.claude/hooks/`` and
> deployed fleet-wide via the ``agents/_base/to_home/`` overlay)
> BLOCKS any unbounded foreground Bash with a WHY message that
> explains the relaunch routes. This skill explains the rationale;
> the hook is the wall. If the hook blocks you, the relaunch
> guidance comes from the hook itself — don't fight it, do not
> bypass except via the documented escape hatch.
>
> **Hook layering** (PreToolUse on the Bash matcher):
>   1. ``enforce_delegation.sh`` (lead-side, dotfiles) — fires only
>      for coordinator roles (``lead``, ``head-*``, ``telegram``,
>      ``proj-*``). Adds the coordinator-extra "delegate to a SAC
>      peer" guidance on top of the bounded-foreground rule.
>   2. ``force_background_bash.sh`` (this hook, role-agnostic) —
>      fires for every agent regardless of role, applies the same
>      bounded-foreground policy verbatim. The fleet's safety net.
>   3. ``force_background_agents_always.sh`` (sibling, Agent/Task
>      matcher) — forces every Agent/Task subagent invocation to use
>      ``run_in_background=true`` so the parent never blocks on
>      child work.
>
> The three hooks compose: 1 + 2 give "no unbounded foreground
> Bash"; 3 gives "no foreground subagents".

## The problem (why this rule exists)

The conversation runner reads its inbox sequentially:
``_runners/_session_conversation.py`` blocks on ``await inbox.get()``
between turns. Telegram messages, A2A turns, and the autonomous loop
all post ``TurnEnvelope``s onto the SAME inbox. While ``_drive_turn``
is running, new envelopes are accepted and queued but **NOT
processed** until the current turn finishes.

So if your turn is a 4-minute LaTeX compile, the operator's "stop
that and check this instead" arrives, sits in the queue, and is
answered four minutes later. The operator hits this repeatedly and
loses trust in the agent.

The fix is not a smarter interrupt — it is a shorter turn.

## The rule

**Never block the main turn on long-running work.** Launch it in the
background, end the turn promptly, and handle the result when the
notification fires.

### Heavy work that MUST go to the background

| Operation | Tool | Background invocation |
|---|---|---|
| ``pytest`` (anything > 7s) | Bash | ``Bash(... , run_in_background=True)`` |
| LaTeX compile (``pdflatex`` / ``xelatex`` / ``latexmk`` / ``tectonic``) | Bash | ``Bash(... , run_in_background=True)`` |
| Figure build, mermaid render, nbconvert | Bash | ``Bash(... , run_in_background=True)`` |
| ``make`` / ``cmake`` / ``ninja`` / ``sphinx-build`` | Bash | ``Bash(... , run_in_background=True)`` |
| Training / inference / large numpy jobs | Bash | ``Bash(... , run_in_background=True)`` |
| Big git ops (clone of multi-GB repo, ``git filter-repo``) | Bash | ``Bash(... , run_in_background=True)`` |
| Apptainer / Docker image build | Bash | ``Bash(... , run_in_background=True)`` |
| ``cargo`` / ``npm`` / ``yarn`` / ``pnpm`` / ``pip install`` | Bash | ``Bash(... , run_in_background=True)`` |
| ``jupyter`` / nbconvert long render | Bash | ``Bash(... , run_in_background=True)`` |
| ``sleep N`` for N ≥ 1 | Bash | ``Bash(... , run_in_background=True)`` |
| Multi-step research / code-review delegation | Agent | ``Agent(... , run_in_background=True)`` |
| ``gh pr checks --watch`` / CI poll | Bash | ``Bash(... , run_in_background=True)`` |
| Any download > 100 MB | Bash | ``Bash(... , run_in_background=True)`` |
| Anything chained with ``&&`` / ``||`` / ``;`` or piped | Bash | ``Bash(... , run_in_background=True)`` |

If you are not sure whether the job is heavy, assume it is. The cost
of mis-classifying a 2-second job as background is one extra
notification round-trip; the cost of mis-classifying a 4-minute job
as foreground is operator frustration.

### Foreground is fine for

Short trivial checks — defined by the hook as ≤50 characters, no
pipe/redirect/chain (``|`` / ``<`` / ``>`` / ``&&`` / ``;``), and
no first-token in the known long-runner set:

- Single ``Read`` / ``Edit`` / ``Write`` / ``Grep`` calls (non-Bash).
- ``pwd``, ``date``, ``whoami``, ``hostname``, ``id``, ``uname``.
- ``ls -la``, ``cat /etc/hostname``, ``echo ...``, ``head -5 file``.
- ``git -C /work status -s``, ``git log --oneline -5``, ``git diff HEAD~1``.
- Anything wrapped in ``timeout N`` where N is 1–7 (with or without
  the ``s`` suffix).

If the trivial-foreground check fails, the hook blocks you and asks
you to relaunch via mechanism (1)–(4) above.

## The shape of a responsive turn

```
Turn N    (operator: "audit the build")
  ├─ Acknowledge in one line via Telegram reply.
  ├─ Spawn Agent(run_in_background=true, prompt="audit ...").
  └─ END THE TURN. ← critical
        (Inbox is now free; if the operator follows up,
         it is processed immediately.)

Turn N+1  (notification: agent completed)
  ├─ Read the agent's report.
  ├─ Relay summary to operator via Telegram.
  └─ END THE TURN.
```

The wrong shape — and the one that produces operator frustration:

```
Turn N    (operator: "audit the build")
  ├─ Acknowledge.
  ├─ Run pytest in the foreground for 90 seconds.
  ├─ Run gh pr checks --watch for 3 minutes.
  ├─ Compile LaTeX for another 4 minutes.
  └─ ... operator's "actually never mind, check X" sat in the queue
        the whole time.
```

## Available mechanisms (in claude-agent-sdk + sac runner)

Verified for the bundled Claude Code CLI ``2.1.150`` shipped with
``claude-agent-sdk 0.2.87`` (the version sac runs inside its SIF):

1. **``Bash(..., run_in_background=True)``** — primary; ~95% of
   cases. Pure shell command (LaTeX compile, image build, ``pytest``,
   training, CI watch, large download). The CLI binary 2.1.150
   exposes ``run_in_background`` on Bash; the sac runner does not
   strip the parameter (``runtimes/_sdk_common.py:475-478`` only
   appends "Agent" to ``allowed_tools``). The runtime delivers a
   completion notification with stdout/stderr on a later turn.

2. **``setsid nohup <cmd> >log 2>&1 </dev/null &``** — explicit
   detach. Use when the job must survive the agent process itself
   restarting (e.g. a multi-hour training that should outlive a
   credential rotation). Pair with a ``tail`` / status-file pattern
   on a later turn to pick up results. Recognised by the
   ``force_background_bash.sh`` hook as a valid background route.

3. **``Task`` / ``Agent(..., run_in_background=True)``** — for
   genuine MULTI-STEP delegated work (research, audits, code
   review, compile-and-report). The runner explicitly enables
   Subagents (same line above); the CLI exposes ``run_in_background``
   on Task. Don't spawn a subagent just to run pytest — use (1).
   Right when the work has reasoning around it.

4. **``timeout N <cmd>``** where N is 1–7 seconds — bounded
   foreground. The ``force_background_bash.sh`` hook treats this as
   an allowed foreground variant. Use only when you genuinely
   expect the command to finish in ≤7s.

5. **Full-agent peer delegation (`sac peer post-turn ...`)** — for
   the heaviest, longest-horizon work (days). Spawns another sac
   agent. See ``18_full-agent-delegation.md``.

Pick the highest-level mechanism that fits. (1) is almost always
right; (3) when the work has multi-step reasoning around it; (2)
when the job must survive a restart.

## What "end the turn promptly" means

End the turn as soon as you have:

1. Acknowledged the request (one short reply to the operator).
2. Spawned the background work.
3. Done any sub-5-second foreground prep needed to start the
   background job correctly.

Do **not** stay in the turn to "watch" the background job. The
runtime delivers a ``<task-notification>`` (for Agent runs) or a
shell-completion notification (for Bash ``run_in_background``)
automatically; you will be woken when there is something to do.

## Cache + responsiveness side benefit

Short turns also keep the Anthropic prompt cache warm. The TTL is
five minutes; a long uninterrupted turn that reads many files at the
start eats the cache budget even when the model is idle between tool
calls. Short turns + background offload keep more of the next turn's
input on cache hits.

## Cross-references

- ``18_full-agent-delegation.md`` — full-agent peer pattern (also
  inherently async; the launcher's turn ends, the peer runs for hours).
- ``17_inbound-turn-endpoint.md`` — how ``POST /v1/turn`` envelopes
  reach the same inbox Telegram posts to (so a short turn matters
  for *any* inbound, not just operator).
- ``29_progress-reporting-to-lead.md`` — the lead is also a consumer
  of "agent reachable now" — push milestones, don't make the lead
  poll while you sit in a 4-minute compile.
- ``23_telegram-integration.md`` — the Telegram delivery path that
  this rule exists to keep responsive.
