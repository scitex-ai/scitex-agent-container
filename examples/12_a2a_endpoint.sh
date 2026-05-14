#!/usr/bin/env bash
# Lesson 12 — The A2A endpoint (agent-to-agent HTTP, A2A v1.0).
#
# What problem does this solve?
#   Once you have more than one agent, they need to talk to each
#   other. tmux pane addressing doesn't scale and isn't routable.
#   sac's answer: each agent is reachable over A2A v1.0 — the
#   Linux-Foundation-standard agent-to-agent HTTP protocol. Any A2A
#   v1.0 client (curl, Google ADK, LangGraph, CrewAI, LlamaIndex,
#   Microsoft Agent Framework) can POST `SendMessage` to a sac agent
#   and get a Task back. No sac-only glue required.
#
# Failure mode if you skip this:
#   - You write fragile glue that pipes prompts through `sac agents
#     send` from a parent process. That works for 2 agents; at 10
#     you want a real protocol.
#   - You miss the AgentCard at /.well-known/agent-card.json (note:
#     v1.0 renamed it from agent.json), which advertises capabilities
#     so other agents can DISCOVER you.
#
# spec.yaml fragment:
#   spec:
#     a2a:
#       handler: claude_session # echo | claude_session | claude_cli | exec
#       port: 8888
#
# Endpoints sac exposes (per ADR-0004 — A2A v1.0 compliance):
#   GET  /.well-known/agent-card.json
#        → fleet card (sac extension lists members under
#          `x-scitex-agent-container.agents[]`; A2A v1 has no
#          multi-agent directory primitive)
#   GET  /agents/<name>/.well-known/agent-card.json
#        → per-agent v1 AgentCard
#   POST /agents/<name>/message:send
#        → A2A v1 REST binding for SendMessage / SendStreamingMessage
#          / GetTask / CancelTask. Body is JSON-RPC; the SDK
#          dispatches by `method`.
#   GET  /agents/<name>/inbox/stream
#        → SSE push of inbound message events (sac extension;
#          consumed by `sac mcp channel` for in-session push to
#          claude)
#
# Wire details that trip people up:
#   - `A2A-Version: 1.0` header is REQUIRED. Without it the SDK
#     assumes v0.3 and rejects.
#   - sac-extension fields (from_agent, conversation_id, priority,
#     in_reply_to, requires_reply) live in `params.metadata`, NOT
#     at the params root. The SDK strict-validates against the v1
#     proto and rejects unknown top-level params fields.
#
# Inter-agent send (high-level):
#   Tell an agent to invoke its sac MCP `a2a_send` tool. The
#   sidecar POSTs /agents/<target>/message:send on its behalf.
#   See lesson 20 (talking_with_agents.sh) for the full demo.
#
# Pure-apptainer equivalent:
#   None. apptainer has no notion of inter-instance messaging. You
#   would have to embed an HTTP server in your %runscript and manage
#   ports / discovery / auth yourself.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-alpha}"
PORT="${SAC_DEMO_A2A_PORT:-8888}"
BASE="http://127.0.0.1:$PORT"

echo "── spec.yaml fragment to enable A2A v1.0 ──"
cat <<YAML
spec:
  runtime: apptainer
  a2a:
    handler: claude_session
    port: $PORT
  claude:
    channels:
      - server:sac        # auto-wires the sac MCP push sidecar
YAML

echo
echo "── (A) Curl the fleet AgentCard ──"
echo '$ curl -s '"$BASE"'/.well-known/agent-card.json | jq .'
echo '  # → v1-shaped card with supportedInterfaces[]; sac members listed'
echo '  #   under x-scitex-agent-container.agents[]'

echo
echo "── (B) Curl a specific agent's card ──"
echo '$ curl -s '"$BASE"'/agents/'"$DEMO_NAME"'/.well-known/agent-card.json | jq .'
echo '  # → v1 AgentCard with capabilities.pushNotifications=true and'
echo '  #   capabilities.extensions[] advertising the sac MCP push channel'

echo
echo "── (C) Send a message (the raw A2A v1.0 wire) ──"
cat <<JSON
\$ curl -sX POST $BASE/agents/$DEMO_NAME/message:send \\
    -H 'Content-Type: application/json' \\
    -H 'A2A-Version: 1.0' \\
    -d '{
      "jsonrpc":"2.0","id":"1","method":"SendMessage",
      "params":{
        "message":{"message_id":"m1","role":"ROLE_USER",
                   "parts":[{"text":"Hello via A2A v1.0"}]},
        "metadata":{"from_agent":"operator"}
      }
    }'
  # → JSON-RPC envelope with result.task.{id, status, artifacts}
  # → matching push event also fires on /agents/$DEMO_NAME/inbox/stream
JSON

echo
echo "── (D) Subscribe to inbox SSE ──"
echo '$ curl -N '"$BASE"'/agents/'"$DEMO_NAME"'/inbox/stream'
echo '  # → keeps the connection open; each POST to the agent emits one'
echo "  #   SSE 'event: message' frame carrying msg_id / from_agent / content"

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── Probing $BASE/.well-known/agent-card.json (real) ──"
    if ! curl -sS --max-time 3 "$BASE/.well-known/agent-card.json"; then
        echo
        echo "(no A2A endpoint listening on :$PORT — start one with:"
        echo "   sac a2a serve ~/.scitex/agent-container/agents/$DEMO_NAME/spec.yaml --port $PORT)"
    fi
fi

# EOF
