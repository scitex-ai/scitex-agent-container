---
description: |
  [TOPIC] Multiplexer (vestigial for agents)
  [DETAILS] SAC agents run via the apptainer SDK runtime (runtimes/claude_session.py) — NOT inside a terminal multiplexer. The tmux wrap is the lead session's launcher concern, outside this package. The spec.multiplexer key is a vestigial config field with no live consumer.
tags: [scitex-agent-container-multiplexer]
---

# Multiplexer

**A terminal multiplexer is not part of the SAC agent execution path.**
SAC agents run via the apptainer SDK runtime
(`runtimes/claude_session.py`), which drives `claude-agent-sdk` from a
Python runner — no tmux, no screen, no pane scraping, no `send-keys`
auto-accept. See [15_claude-session.md](15_claude-session.md).

There is no `runtimes/multiplexer.py` module and nothing in the runtime
imports a `get_multiplexer` / `TmuxManager` / `ScreenManager` API; that
surface was removed when the SDK runtime replaced the legacy tmux-wrapped
CLI runtime. The `spec.multiplexer` YAML key still parses (default
`tmux`, in `config/_types.py`) but is **vestigial** — no agent code path
consumes it, so setting it has no effect on how an agent runs.

## Where tmux *is* used

tmux is used only to wrap the **lead** Claude session — for session
continuity (`--continue`) and remote (iPhone) attach. That wrapping lives
in the lead's launcher (`scripts/deployment/claude.sh`, which wraps the
session in a named tmux session `lead`), **outside** this package. It is
an operator convenience for one interactive human-facing session, not an
agent execution mechanism, and SAC does not manage it.

## Attaching to a running agent

Use the SDK runtime's own surfaces instead of `tmux attach`:

- `sac agents tail <name>` — rendered transcript from `session.jsonl`.
- `sac agents start <name> --foreground` — stream a turn to your terminal.
