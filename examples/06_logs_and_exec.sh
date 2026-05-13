#!/usr/bin/env bash
# Lesson 06 — Reading logs and entering a running instance.
#
# What problem does this solve?
#   When an agent "isn't doing what it should", you need to read two
#   different things:
#     1. The process-level stdout/stderr (did the SDK crash? did the
#        container fail to mount $HOME?).
#     2. The structured per-turn transcript (what did Claude actually
#        decide to do? which tool call exploded?).
#   apptainer gives you (1); sac gives you (2) on top.
#
# Failure mode if you skip this:
#   - You spend 20 minutes guessing why the agent is silent, when
#     a 30-line `sac agents tail` would have shown "tool_use blocked
#     by permission prompt" on the first line.
#   - You `kill -9` an agent without reading session.jsonl and lose
#     the trail of what it was about to do.
#
# Pure apptainer:
#   apptainer instance logs <name>            # combined stdout + stderr
#   apptainer instance logs <name> --err      # stderr only
#   apptainer exec instance://<name> bash     # interactive shell inside
#   # → drops you in a shell with the same env / binds as the agent
#
# Logs land at:
#   ~/.apptainer/instances/logs/<host>/<user>/<name>.{out,err}
#   # → useful when you want to grep across multiple agents at once
#
# sac equivalent:
#   sac agents tail <name>                    # session.jsonl, structured
#   sac agents tail <name> --json             # raw JSONL, one event per line
#   sac agents tail <name> -n 50              # last 50 events
#   sac agents tail <name> --follow           # `tail -f` mode
#   sac agents recall <name>                  # human-readable session summary
#   # → "Last turn: user asked X, assistant called tool Y, returned Z"
#
# Why sac prefers session.jsonl over instance logs:
#   apptainer logs are raw stdout/stderr from the process. Claude SDK
#   writes structured JSON records to session.jsonl describing each
#   turn (user / assistant / tool_use / tool_result / result).
#   Tailing the structured stream tells you WHAT the agent decided,
#   not just what bytes it printed.
#
# session.jsonl layout (one JSON object per line):
#   {"type": "user",       "content": "..."}
#   {"type": "assistant",  "content": [...], "model": "claude-..."}
#   {"type": "tool_use",   "id": "...", "name": "Bash", "input": {...}}
#   {"type": "tool_result","tool_use_id": "...", "content": "..."}
#   {"type": "result",     "stop_reason": "end_turn", "usage": {...}}
#
# Location: ~/.scitex/agent-container/runtime/<name>/session.jsonl
set -euo pipefail

NAME="${SAC_DEMO_AGENT:-hello-agent}"
APPTAINER_LOG_DIR="$HOME/.apptainer/instances/logs"
SESSION_FILE="$HOME/.scitex/agent-container/runtime/$NAME/session.jsonl"

echo "── (A) apptainer instance logs (raw stdout/stderr) ──"
echo '$ apptainer instance logs '"$NAME"
# shellcheck disable=SC2016
echo '$ ls "$APPTAINER_LOG_DIR/$(hostname)/$USER/"'
APPTAINER_LOG_HOST_DIR="$APPTAINER_LOG_DIR/$(hostname)/$USER"
if [[ -d "$APPTAINER_LOG_HOST_DIR" ]]; then
    # shellcheck disable=SC2012
    ls "$APPTAINER_LOG_HOST_DIR/" 2>/dev/null | head -5 || true
else
    echo "(no apptainer logs yet)"
fi

echo
echo "── (B) Enter a running instance ──"
echo '$ apptainer exec instance://'"$NAME"' bash'
echo '  # → interactive shell; ctrl-D to exit'
echo '$ apptainer exec instance://'"$NAME"' env | grep CLAUDE'
echo '  # → see the env the agent actually has'

echo
echo "── (C) sac agents tail (structured) ──"
echo '$ sac agents tail '"$NAME"' -n 5'
sac agents tail "$NAME" -n 5 2>/dev/null || echo "(no transcript for $NAME)"

echo
echo "── (D) Raw session.jsonl (the source of truth) ──"
echo '$ tail -3 '"$SESSION_FILE"' | jq .type'
if [[ -f "$SESSION_FILE" ]]; then
    tail -3 "$SESSION_FILE" 2>/dev/null | head -3
else
    echo "(no session.jsonl — $NAME has never run)"
fi

echo
echo "── (E) Human-readable summary ──"
echo '$ sac agents recall '"$NAME"
echo '  # → "Last user turn: ...; last assistant action: ..."'

# EOF
