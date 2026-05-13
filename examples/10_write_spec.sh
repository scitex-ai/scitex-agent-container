#!/usr/bin/env bash
# Lesson 10 — Writing your first spec.yaml from scratch.
#
# What problem does this solve?
#   The README quickstart hands you a finished spec.yaml. This lesson
#   builds one field by field so you know what each line buys you.
#
# Failure mode if you skip this:
#   - Cargo-culting fields you don't understand (model: opus, restart:
#     always, --dangerously-skip-permissions in production).
#   - Misnaming the agent: the directory name IS the agent name. Naming
#     the dir "my-agent-1" but writing `name: my-agent` somewhere will
#     silently confuse you — there is no `name:` field in v3.
#
# The "dir-as-SSoT" rule:
#   ~/.scitex/agent-container/agents/<NAME>/spec.yaml
#                                  ^^^^^^         ^^^^^^^^^^
#                                  agent name     always this filename
#   No metadata.name override. The directory IS the truth.
#
# The bare-minimum spec.yaml (every other field defaults sensibly):
#
#   apiVersion: scitex-agent-container/v3
#   kind: Agent
#   spec:
#     runtime: apptainer
#     apptainer:
#       image: ~/.scitex/agent-container/containers/sac-base.sif
#     claude:
#       model: haiku
#       flags: [--dangerously-skip-permissions]
#
# What each field does:
#   apiVersion              schema version — v3 is current (2026-05-13)
#   kind                    only "Agent" exists today
#   spec.runtime            "apptainer" — the only supported runtime
#   spec.apptainer.image    path to a built SIF (lesson 01 builds these)
#   spec.claude.model       haiku | sonnet | opus | "claude-..."  (versioned id)
#   spec.claude.flags       passed through to `claude` CLI
#                           --dangerously-skip-permissions = no prompts
#
# Useful additions (annotated full example: examples/agents/full-agent/spec.yaml):
#   spec.workdir            host dir mounted at /work inside the container
#   spec.dot_claude         dir merged into agent workspace (lesson 11)
#   spec.startup_prompts    first user turn after start
#   spec.startup_commands   shell to run before claude boots
#   spec.health             liveness probe (lesson 13)
#   spec.restart            restart policy (lesson 13)
#   spec.a2a.port           HTTP A2A endpoint (lesson 12)
#   spec.host / spec.hosts  cross-host placement (lesson 14)
#
# Pure-apptainer equivalent of "writing a spec":
#   There isn't one — you'd hand-craft a bash wrapper that calls
#   `apptainer instance start ... && claude ...` with the right binds
#   and env. spec.yaml exists to declare this once and let sac
#   materialise it correctly every time.
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-tutorial-demo}"
AGENT_HOME="$HOME/.scitex/agent-container/agents/$DEMO_NAME"
SPEC_FILE="$AGENT_HOME/spec.yaml"

echo "── Target agent directory ──"
echo "  $AGENT_HOME"
echo "  (dir name '$DEMO_NAME' will become the agent name)"

echo
echo "── Minimal spec.yaml to write ──"
cat <<'YAML'
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions
  startup_prompts:
    - "Reply with the single word READY and nothing else."
YAML

echo
echo "── Validate before you start ──"
echo '$ sac agents check '"$DEMO_NAME"
echo '  # → parses spec.yaml, probes that the SIF exists and apptainer is on PATH'
echo '  # → exit 0 = good to start; non-zero = print the field that is wrong'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── Materialising $SPEC_FILE (real) ──"
    mkdir -p "$AGENT_HOME"
    cat >"$SPEC_FILE" <<'YAML'
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions
  startup_prompts:
    - "Reply with the single word READY and nothing else."
YAML
    echo
    echo "── sac agents check $DEMO_NAME (real) ──"
    sac agents check "$DEMO_NAME" || true
fi

# EOF
