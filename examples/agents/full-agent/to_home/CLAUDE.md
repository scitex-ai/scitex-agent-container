# Agent Role

<!-- Describe the agent's role and responsibilities here. -->

This agent is a general-purpose worker.

## Context

- Project: (fill in)
- Workdir: (mounted at /work inside the container)

## Behaviour

- Follow instructions precisely.
- Commit and push changes when asked.

## Responsiveness — short turns, background heavy work

The operator's Telegram message reaches this agent via the same inbox
the conversation reads. While a turn is in flight, inbound messages
queue and are NOT processed until the turn ends. So a 4-minute LaTeX
compile in the foreground means a 4-minute Telegram delay — which is
the operator's #1 UX pain.

**Default rule:** never block the main turn on long-running work.
Launch it in the background, end the turn promptly, and handle the
result when the runtime delivers the completion notification. The
work itself is NOT interrupted; it CONTINUES off the main loop. The
operator was explicit (8843): "作業中断はしてほしくない" — don't
interrupt the work; just keep the main loop free.

Heavy work → background. In order of preference:

  (a) **`Bash(..., run_in_background=True)`** — primary; ~95% of
      cases. Pure shell command (LaTeX compile, figure / mermaid
      render, image build, `pytest` longer than ~7s, training /
      inference, large data jobs, CI watches like `gh pr checks
      --watch`, large downloads). SDK delivers a completion
      notification on a later turn.

  (b) **`setsid nohup <cmd> >log 2>&1 </dev/null &`** — explicit
      detach when the job must survive the agent process restarting
      (multi-hour training across credential rotations).

  (c) **`Task` / `Agent(..., run_in_background=True)`** — for
      genuine MULTI-STEP delegated work (research, audits, code
      review, compile-and-report). Don't spawn a subagent just to
      run pytest — use (a).

  (d) **`timeout 7 <cmd>`** — if the command truly finishes in ≤7
      seconds, just bound it.

Foreground is fine for short trivial checks (≤50 chars, no pipe /
redirect / chain, no known long-runner first token): `pwd`, `date`,
`git -C /work status -s`, `ls -la`.

After spawning background work: acknowledge once, then **end the
turn**. Do not stay in the turn to watch progress — the runtime
wakes you on completion.

Doctrine + full mechanism ladder + cross-references:
`~/.claude/skills/scitex/scitex-agent-container/30_responsiveness-background-work.md`.

This rule is **structurally enforced** by the pre-tool-use hook
`~/.claude/hooks/pre-tool-use/force_background_bash.sh` (shipped in
this template). An unbounded foreground Bash is BLOCKED with a WHY
message + relaunch guidance. Escape hatch (rare):
`CC_ALLOW_FOREGROUND_HEAVY=1`.
