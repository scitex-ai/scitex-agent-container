#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-05-14 19:27:15 (ywatanabe)"
# File: ./examples/20_talking_with_agents.sh
#
# Lesson 20 — Talking with agents over A2A v1.0 (+ sac MCP push).
#
# What this verifies, end-to-end:
#   1. Two sac agents (alpha, beta) registered as a multi-tenant
#      fleet under one `sac a2a serve` process on :8888.
#   2. The fleet card at /.well-known/agent-card.json is A2A v1.0
#      shaped (no top-level url / authentication /
#      stateTransitionHistory; supportedInterfaces[] present;
#      version matches the v<N> prefix).
#   3. Per-agent cards advertise the sac MCP push channel under
#      capabilities.extensions[] AND pushNotifications=true.
#   4. The on-the-wire v1 validator rejects a v0-shaped card.
#   5. A real `SendMessage` round-trip drives the agent's claude
#      session AND the inbox SSE delivers the matching push event.
#   6. Alpha can call its `a2a_send` MCP tool to message beta
#      (interactive — two-shell recipe printed).
#
# Prereqs:
#   * Agent YAMLs at ~/.scitex/agent-container/agents/{alpha,beta}
#     with spec.a2a.handler=claude_session, spec.a2a.port=8888,
#     spec.claude.channels=[server:sac].
#   * Server running:
#       sac a2a serve \
#         ~/.scitex/agent-container/agents/alpha/spec.yaml \
#         ~/.scitex/agent-container/agents/beta/spec.yaml \
#         --port 8888
#
# Pass --apply to execute the live probes (steps 1–5). Step 5
# takes 5–15 seconds (real claude turn). Step 6 is interactive.

ORIG_DIR="$(pwd)"
THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="$THIS_DIR/.$(basename "$0").log"
echo >"$LOG_PATH"

# shellcheck disable=SC2034
GIT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"

GRAY='\033[0;90m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo_info() { echo -e "${GRAY}INFO: $1${NC}"; }
echo_success() { echo -e "${GREEN}SUCC: $1${NC}"; }
echo_warning() { echo -e "${YELLOW}WARN: $1${NC}"; }
echo_error() { echo -e "${RED}ERRO: $1${NC}"; }
echo_header() { echo_info "=== $1 ==="; }
# ---------------------------------------

set -uo pipefail
APPLY="${1:-}"
BASE="${SAC_A2A_BASE:-http://127.0.0.1:8888}"
PY="${SAC_PY:-/home/ywatanabe/.venv/bin/python}"

# Show the recipe (always — works as live docs even without --apply).

echo_header "(1) Unified version prefix (^scitex-agent-container/v\\d+$)"
echo '$ for n in "" alpha beta; do'
echo "    curl -s $BASE/\${n:+agents/\$n/}.well-known/agent-card.json | jq -r .version"
echo '  done'
echo '  # → scitex-agent-container/v1   (fleet)'
echo '  # → scitex-agent-container/v3   (alpha)'
echo '  # → scitex-agent-container/v3   (beta)'

echo
echo_header "(2) Fleet card is v1-shaped"
echo "v0 fields (top-level url, authentication, stateTransitionHistory)"
echo "must be absent. supportedInterfaces[] must be present."
echo
cat <<PY1
\$ $PY <<'PY'
import urllib.request, json
with urllib.request.urlopen('$BASE/.well-known/agent-card.json') as r:
    c = json.load(r)
print('top-level url:           ', 'url' in c)                                    # False
print('authentication:          ', 'authentication' in c)                         # False
print('stateTransitionHistory:  ', 'stateTransitionHistory' in c.get('capabilities', {}))  # False
print('supportedInterfaces[0]:  ', c['supportedInterfaces'][0])
print('extensions[]:            ', [e['uri'] for e in c['capabilities']['extensions']])
PY
PY1

echo
echo_header "(3) Per-agent card surfaces the sac push channel"
echo "When spec.claude.channels contains server:sac, the card advertises"
echo "pushNotifications=true AND a capabilities.extensions[] entry naming"
echo "the SSE path and the MCP tools the sidecar registers."
echo
cat <<PY2
\$ $PY <<'PY'
import urllib.request, json
with urllib.request.urlopen('$BASE/agents/alpha/.well-known/agent-card.json') as r:
    c = json.load(r)
print('pushNotifications:', c['capabilities']['pushNotifications'])              # True
ext = c['capabilities']['extensions'][0]
print('extension uri:    ', ext['uri'])
print('sse path:         ', ext['params']['sse_path'])
print('mcp tools:        ', ext['params']['mcp_tools'])
PY
PY2

echo
echo_header "(4) The v1 validator rejects v0-shaped cards"
echo "Every card served runs through validate_card_v1() — this proves"
echo "the validator catches a bad shape before it reaches a client."
echo
cat <<PY3
\$ $PY <<'PY'
from scitex_agent_container.a2a._card import validate_card_v1, CardSchemaError
bad = {
    "name": "x", "url": "http://example", "version": "1",          # v0 url
    "authentication": {"schemes": ["none"]},                         # v0 auth
    "supportedInterfaces": [{"url": "http://x", "protocolBinding": "HTTP+JSON",
                             "tenant": "x", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False,
                     "extendedAgentCard": False},
    "skills": [], "defaultInputModes": [], "defaultOutputModes": [],
}
try:
    validate_card_v1(bad)
except CardSchemaError as exc:
    print('rejected as expected:', str(exc)[:120])
PY
PY3

echo
echo_header "(5) Round-trip SendMessage; inbox SSE fires"
echo "Layer-1 wire: POST /agents/<name>/message:send."
echo "sac-extension fields (from_agent, priority, ...) live in"
echo "params.metadata — the SDK rejects unknown fields at params root."
echo
echo "Shell A (hold open):"
echo "  curl -N $BASE/agents/alpha/inbox/stream"
echo
echo "Shell B:"
cat <<JSON
  curl -s -X POST $BASE/agents/alpha/message:send \\
    -H "Content-Type: application/json" -H "A2A-Version: 1.0" \\
    -d '{
      "jsonrpc":"2.0","id":"t1","method":"SendMessage",
      "params":{
        "message":{"message_id":"m-t1","role":"ROLE_USER",
                   "parts":[{"text":"Reply READY."}]},
        "metadata":{"from_agent":"operator","priority":"normal"}
      }
    }' | jq .result.task.status.message.parts[0]
JSON

echo
echo_header "(6) Agent-to-agent via the a2a_send MCP tool"
echo "Tell alpha to invoke its sac MCP tool. The sidecar POSTs"
echo "/agents/beta/message:send on alpha's behalf; beta's inbox fires."
echo
echo "Shell A (hold open):"
echo "  curl -N $BASE/agents/beta/inbox/stream"
echo
echo "Shell B:"
cat <<JSON
  curl -s -X POST $BASE/agents/alpha/message:send \\
    -H "Content-Type: application/json" -H "A2A-Version: 1.0" \\
    -d '{
      "jsonrpc":"2.0","id":"t2","method":"SendMessage",
      "params":{
        "message":{"message_id":"m-t2","role":"ROLE_USER","parts":[{"text":
          "Use the a2a_send MCP tool to send beta the single word: hi"
        }]},
        "metadata":{"from_agent":"operator"}
      }
    }' | jq .result.task.status.message.parts[0]
JSON

if [[ "$APPLY" != "--apply" ]]; then
    echo
    echo_info "Re-run with --apply to execute steps 1-5 live."
    exit 0
fi

# ----- LIVE PROBES (--apply) -----

echo
echo_header "[APPLY] reachability"
if ! curl -sS --max-time 3 "$BASE/agents/" >/dev/null; then
    echo_error "$BASE/agents/ is not reachable. Start the server with:"
    echo "  sac a2a serve ~/.scitex/agent-container/agents/alpha/spec.yaml \\"
    echo "                ~/.scitex/agent-container/agents/beta/spec.yaml --port 8888"
    exit 1
fi
echo_success "$BASE/agents/ responding"

echo
echo_header "[APPLY] (1) version unification"
"$PY" <<PY || {
import re, urllib.request, json, sys
pat = re.compile(r"^scitex-agent-container/v\d+$")
for tag, path in (
    ("fleet", "/.well-known/agent-card.json"),
    ("alpha", "/agents/alpha/.well-known/agent-card.json"),
    ("beta",  "/agents/beta/.well-known/agent-card.json"),
):
    with urllib.request.urlopen("$BASE" + path) as r:
        v = json.load(r)["version"]
    print(f"  {tag:>6}: {v}")
    if not pat.match(v):
        print(f"FAIL: {tag} version {v!r} doesn't match {pat.pattern}", file=sys.stderr); sys.exit(1)
print("ok — all versions match v<N> convention")
PY
    echo_error "version check failed"
    exit 1
}

echo_success "(1) ok"

echo
echo_header "[APPLY] (2) fleet v1 shape"
"$PY" <<PY || {
import urllib.request, json, sys
with urllib.request.urlopen("$BASE/.well-known/agent-card.json") as r:
    c = json.load(r)
errors = []
if "url" in c: errors.append("top-level url present (v0)")
if "authentication" in c: errors.append("authentication present (v0)")
if "stateTransitionHistory" in c.get("capabilities", {}):
    errors.append("stateTransitionHistory present (v0)")
if "supportedInterfaces" not in c:
    errors.append("supportedInterfaces missing (v1 REQUIRED)")
if errors:
    for e in errors: print(f"FAIL: {e}", file=sys.stderr)
    sys.exit(1)
print(f"  supportedInterfaces[0].url     = {c['supportedInterfaces'][0]['url']}")
print(f"  capabilities.extensions[0].uri = {c['capabilities']['extensions'][0]['uri']}")
print("ok — fleet card is v1-shaped")
PY
    echo_error "fleet shape check failed"
    exit 1
}

echo_success "(2) ok"

echo
echo_header "[APPLY] (3) per-agent push channel surfaced"
"$PY" <<PY || {
import urllib.request, json, sys
with urllib.request.urlopen("$BASE/agents/alpha/.well-known/agent-card.json") as r:
    c = json.load(r)
if not c["capabilities"]["pushNotifications"]:
    print("FAIL: pushNotifications is False; expected True", file=sys.stderr); sys.exit(1)
ext = c["capabilities"]["extensions"][0]
if "sac-push-channel" not in ext["uri"]:
    print(f"FAIL: extension uri {ext['uri']!r} is not the sac push channel", file=sys.stderr); sys.exit(1)
print("  pushNotifications:", c["capabilities"]["pushNotifications"])
print("  extension uri:    ", ext["uri"])
print("  sse path:         ", ext["params"]["sse_path"])
print("  mcp tools:        ", ext["params"]["mcp_tools"])
print("ok — push channel surfaced on the card")
PY
    echo_error "per-agent push check failed"
    exit 1
}

echo_success "(3) ok"

echo
echo_header "[APPLY] (4) validator rejects v0-shaped card"
"$PY" <<'PY' || {
from scitex_agent_container.a2a._card import validate_card_v1, CardSchemaError
bad = {"name":"x","url":"http://example","version":"1",
       "authentication":{"schemes":["none"]},
       "supportedInterfaces":[{"url":"http://x","protocolBinding":"HTTP+JSON",
                               "tenant":"x","protocolVersion":"1.0"}],
       "capabilities":{"streaming":False,"pushNotifications":False,"extendedAgentCard":False},
       "skills":[],"defaultInputModes":[],"defaultOutputModes":[]}
try:
    validate_card_v1(bad)
except CardSchemaError as exc:
    print("ok — validator rejected v0 card:", str(exc)[:100])
else:
    raise SystemExit("FAIL: validator accepted a v0-shaped card")
PY
    echo_error "validator check failed"
    exit 1
}

echo_success "(4) ok"

echo
echo_header "[APPLY] (5) SendMessage round-trip + inbox SSE"
"$PY" <<PY || {
import json, threading, time, urllib.request, sys

events = []
sse_done = threading.Event()
def _consume():
    req = urllib.request.Request("$BASE/agents/alpha/inbox/stream")
    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
                sse_done.set()
                return
threading.Thread(target=_consume, daemon=True).start()
time.sleep(0.5)

body = json.dumps({
    "jsonrpc":"2.0","id":"apply-5","method":"SendMessage",
    "params":{
        "message":{"message_id":"m-apply-5","role":"ROLE_USER",
                   "parts":[{"text":"Reply with just READY."}]},
        "metadata":{"from_agent":"operator","priority":"normal"},
    },
}).encode()
req = urllib.request.Request("$BASE/agents/alpha/message:send",
    data=body, method="POST",
    headers={"Content-Type":"application/json","A2A-Version":"1.0"})
with urllib.request.urlopen(req, timeout=60) as r:
    resp = json.load(r)

if not sse_done.wait(timeout=10):
    print("FAIL: no SSE event received", file=sys.stderr); sys.exit(1)
ev = events[0]
if ev.get("from_agent") != "operator":
    print(f"FAIL: SSE from_agent={ev.get('from_agent')!r}", file=sys.stderr); sys.exit(1)
task = resp["result"]["task"]
state = task["status"]["state"]
reply = task["status"]["message"]["parts"][0]["text"]
if "COMPLETED" not in state:
    print(f"FAIL: task state {state!r}", file=sys.stderr); sys.exit(1)
print(f"  SSE   from_agent={ev['from_agent']!r}  msg_id={ev['msg_id']}")
print(f"  reply {reply!r}  state={state}")
print("ok — wire + push roundtrip green")
PY
    echo_error "round-trip check failed"
    exit 1
}

echo_success "(5) ok"

echo
echo_success "All apply steps passed."
echo_info "Step 6 (agent-to-agent via a2a_send) is interactive — see the two-shell recipe above."

cd "$ORIG_DIR" || exit 1

# EOF
