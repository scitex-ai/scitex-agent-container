#!/usr/bin/env bash
# Lesson 15 — Debugging a broken agent: a checklist.
#
# What problem does this solve?
#   When `sac agents start my-agent` produces no output and `sac agents
#   list` shows status=failed, you need a recipe — not panic. This
#   lesson is that recipe.
#
# Failure mode if you skip this:
#   - You `rm -rf ~/.scitex/agent-container/runtime/<name>/` reflexively
#     and lose the only forensic data you had.
#   - You rebuild the SIF before checking spec.yaml syntax.
#
# The five-step debug checklist:
#
#   1. PREFLIGHT  — does the spec parse?
#        sac agents check <name>
#        # → "ERROR: spec.apptainer.image: file not found ..."
#        # If this fails, fix spec.yaml. Nothing downstream matters.
#
#   2. STATE      — what does sac think happened?
#        ls ~/.scitex/agent-container/runtime/<name>/
#        # → pid heartbeat.json restart.log session.jsonl stderr.log stdout.log
#        cat ~/.scitex/agent-container/runtime/<name>/stderr.log
#        # → first 20 lines usually contain the proximate cause.
#
#   3. TRANSCRIPT — what did Claude actually do/say?
#        sac agents tail <name> --json -n 20
#        # → look for `"type":"result","stop_reason":"error"`.
#        # If session.jsonl is missing entirely, Claude never even
#        # started — you're really debugging the apptainer launch.
#
#   4. CONTAINER  — is the container itself sane?
#        apptainer instance list
#        # → if your agent name is here: container is up but Claude failed.
#        # → if not: apptainer never launched. Look at stderr.log step 2.
#        apptainer exec instance://<name> bash       # interactive shell
#        apptainer exec instance://<name> env        # inspect env / binds
#
#   5. SANDBOX    — make the SIF writable and poke at it.
#        # If you suspect the image itself is wrong (missing lib,
#        # wrong python version), open the sandbox copy:
#        apptainer build --sandbox /tmp/sb.sandbox/ <your.sif>
#        apptainer exec --writable /tmp/sb.sandbox/ bash
#        # → install a missing dep with pip, exit, rebuild SIF.
#        # See lesson 02 for the proper sandbox/update/freeze flow.
#
# Useful one-liners:
#
#   Show the last error a wedged agent emitted:
#     tail -50 ~/.scitex/agent-container/runtime/<name>/stderr.log
#
#   Watch session.jsonl as it grows:
#     tail -F ~/.scitex/agent-container/runtime/<name>/session.jsonl \
#       | jq -c '{type, stop_reason: .stop_reason, content: (.content // null)}'
#
#   Find every agent whose last restart was an OOM:
#     grep -l OOMKilled ~/.scitex/agent-container/runtime/*/restart.log
#
#   Reset an agent that refuses to start because of stale state:
#     sac agents stop  <name> --force
#     sac agents delete <name> -y     # nukes runtime/ but NOT the spec
#     sac agents start <name>
#
# Pure-apptainer equivalent of "session.jsonl":
#   There isn't one. Apptainer logs are raw stdout/stderr; the
#   structured transcript is sac+Claude SDK only. That's why step 3
#   above is sac-specific.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-hello-agent}"
RUNTIME_DIR="$HOME/.scitex/agent-container/runtime/$DEMO_NAME"

echo "── Step 1: sac agents check ──"
echo '$ sac agents check '"$DEMO_NAME"
sac agents check "$DEMO_NAME" 2>/dev/null || echo "(check unavailable or spec missing)"

echo
echo "── Step 2: runtime/ state ──"
echo '$ ls '"$RUNTIME_DIR"'/'
if [[ -d "$RUNTIME_DIR" ]]; then
    # shellcheck disable=SC2012
    ls -la "$RUNTIME_DIR/" 2>/dev/null | head -15
else
    echo "(no runtime dir — agent never started)"
fi

echo
echo "── Step 3: structured transcript ──"
echo '$ sac agents tail '"$DEMO_NAME"' --json -n 5'

echo
echo "── Step 4: apptainer instance status ──"
echo '$ apptainer instance list | grep '"$DEMO_NAME"
apptainer instance list 2>/dev/null | grep -E "(INSTANCE|$DEMO_NAME)" || echo "(no matching instance)"

echo
echo "── Step 5: writable sandbox (last resort) ──"
echo '$ apptainer build --sandbox /tmp/sb.sandbox/ ~/.scitex/agent-container/containers/sac-base.sif'
echo '$ apptainer exec --writable /tmp/sb.sandbox/ bash'
echo '  # → fix env, then freeze back with: apptainer build out.sif /tmp/sb.sandbox/'

if [[ "$APPLY" == "--apply" && -d "$RUNTIME_DIR" ]]; then
    echo
    echo "── Last 20 lines of stderr.log (real) ──"
    [[ -f "$RUNTIME_DIR/stderr.log" ]] && tail -20 "$RUNTIME_DIR/stderr.log" || echo "(no stderr.log)"
fi

# EOF
