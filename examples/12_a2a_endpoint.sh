#!/usr/bin/env bash
# Lesson 12 — The A2A endpoint (agent-to-agent HTTP).
#
# What problem does this solve?
#   Once you have more than one agent, they need to talk to each
#   other. tmux pane addressing doesn't scale and isn't routable.
#   sac's answer: each agent can bind a tiny HTTP server on a
#   localhost port. Any process — another agent, a script, curl —
#   POSTs a turn at it and the response flows out via session.jsonl.
#   This is sac's standout feature; no other apptainer wrapper has it.
#
# Failure mode if you skip this:
#   - You write fragile glue that pipes prompts through `sac agents
#     send` from a parent process. That works for 2 agents; at 10
#     you want a real protocol.
#   - You miss the AgentCard at /.well-known/agent.json, which
#     advertises capabilities so other agents can DISCOVER you.
#
# spec.yaml fragment:
#   spec:
#     a2a:
#       port: 7901              # bind on 127.0.0.1:7901
#
# Endpoints sac exposes (see src/.../a2a/_server.py):
#   GET  /.well-known/agent.json
#        → fleet AgentCard (all agents on this host)
#   GET  /agents/<name>/.well-known/agent.json
#        → per-agent AgentCard
#   POST /v1/turn
#        → send a new user turn to this agent
#        body: {"role": "user", "content": "...", "session_id": "..."}
#
# These are the conventions A2A protocol consumers expect.
#
# Inter-agent send (high-level):
#   sac peer post-turn <agent-name> "<prompt>"
#   # → looks up the agent's a2a.port in the registry, POSTs /v1/turn
#
# Pure-apptainer equivalent:
#   None. apptainer has no notion of inter-instance messaging. You
#   would have to embed an HTTP server in your %runscript and manage
#   ports / discovery / auth yourself.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-hello-agent}"
PORT="${SAC_DEMO_A2A_PORT:-7901}"

echo "── spec.yaml fragment to enable A2A ──"
cat <<YAML
spec:
  runtime: apptainer
  a2a:
    port: $PORT
YAML

echo
echo "── (A) Curl the fleet AgentCard ──"
echo '$ curl -s http://127.0.0.1:'"$PORT"'/.well-known/agent.json | jq .'
echo '  # → {"name": "fleet", "agents": [{"name": "'"$DEMO_NAME"'", ...}]}'

echo
echo "── (B) Curl a specific agent's card ──"
echo '$ curl -s http://127.0.0.1:'"$PORT"'/agents/'"$DEMO_NAME"'/.well-known/agent.json | jq .'
echo '  # → {"name": "'"$DEMO_NAME"'", "url": "...", "capabilities": {...}}'

echo
echo "── (C) Post a turn (the raw protocol) ──"
cat <<JSON
\$ curl -sX POST http://127.0.0.1:$PORT/v1/turn \\
    -H 'content-type: application/json' \\
    -d '{"role":"user","content":"Hello via A2A"}'
  # → 202 Accepted, body: {"status": "queued", "turn_id": "..."}
  # → assistant reply appears in session.jsonl
JSON

echo
echo "── (D) High-level sac wrapper ──"
echo '$ sac peer post-turn '"$DEMO_NAME"' "Hello via A2A"'
echo '$ sac agents tail '"$DEMO_NAME"' --json -n 5'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── Probing http://127.0.0.1:$PORT/.well-known/agent.json (real) ──"
    curl -sS --max-time 3 "http://127.0.0.1:$PORT/.well-known/agent.json" ||
        echo "(no A2A endpoint listening on :$PORT — start an agent with spec.a2a.port set)"
fi

# EOF
