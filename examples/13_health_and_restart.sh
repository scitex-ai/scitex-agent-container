#!/usr/bin/env bash
# Lesson 13 — Health probes and restart policy.
#
# What problem does this solve?
#   Long-lived agents wedge. The Claude SDK might lose connectivity,
#   the apptainer instance might OOM, the tmux session backing the
#   pane might die. You want sac to:
#     1. Notice  (health probe)
#     2. Decide  (restart policy)
#     3. Act     (relaunch, capped by backoff)
#   without you babysitting.
#
# Failure mode if you skip this:
#   - Agent silently dies overnight; you discover next morning.
#   - Or: restart: always with no backoff — a broken spec causes
#     1000 restart loops per minute, fills the log dir, masks the
#     real bug. Use exponential backoff.
#
# spec.yaml fragment (full version: examples/agents/full-agent/spec.yaml):
#
#   spec:
#     health:
#       enabled: true
#       interval: 60          # seconds between probes
#       timeout: 10           # probe must respond in 10s
#       method: sdk-alive     # ask the SDK "are you alive?"
#     restart:
#       policy: on-failure    # never | on-failure | always
#       max_retries: 3
#       backoff:
#         initial: 10         # first restart after 10s
#         max: 120            # cap at 2 min between retries
#         multiplier: 2       # double each time
#
# What each policy means:
#   never        — agent crashes, sac records it, never restarts.
#                  Use for one-shot jobs / hello-agent.
#   on-failure   — restart only if exit code is non-zero OR health
#                  probe fails. A clean `sac agents stop` does NOT
#                  trigger a restart.
#   always       — restart for any exit, including clean ones. Use
#                  for "should literally never be down" services.
#
# Health methods:
#   sdk-alive    — POST a no-op turn to the SDK; success if it ACKs.
#   (more probes can be added; sdk-alive is the only standard one today.)
#
# What sac writes during operation:
#   ~/.scitex/agent-container/runtime/<name>/heartbeat.json
#     {"ts": "2026-05-13T12:34:56Z", "status": "alive", "method": "sdk-alive"}
#   ~/.scitex/agent-container/runtime/<name>/restart.log
#     each restart event with reason, exit code, backoff used
#
# Pure-apptainer equivalent:
#   apptainer has no health/restart concept. You'd write a systemd
#   unit or a `while true; do apptainer instance start...; sleep N; done`
#   wrapper, then a separate probe in cron. sac replaces all of that
#   with two YAML blocks.
#
# Watching restart happen by hand (the satisfying part):
#   1. Start an agent with restart.policy=on-failure.
#   2. From another terminal: `tmux kill-session -t <agent-name>`
#      (or `kill -9` the apptainer pid)
#   3. Watch sac notice via the health probe, then bring it back.
#      `tail -f ~/.scitex/agent-container/runtime/<name>/restart.log`
set -euo pipefail
APPLY="${1:-}"

DEMO_NAME="${SAC_DEMO_AGENT:-hello-agent}"
RUNTIME_DIR="$HOME/.scitex/agent-container/runtime/$DEMO_NAME"

echo "── spec.yaml fragment for a resilient worker ──"
cat <<'YAML'
spec:
  health:
    enabled: true
    interval: 60
    timeout: 10
    method: sdk-alive
  restart:
    policy: on-failure
    max_retries: 3
    backoff:
      initial: 10
      max: 120
      multiplier: 2
YAML

echo
echo "── Inspect heartbeat (proves health probe is firing) ──"
echo '$ cat '"$RUNTIME_DIR"'/heartbeat.json'
if [[ -f "$RUNTIME_DIR/heartbeat.json" ]]; then
    cat "$RUNTIME_DIR/heartbeat.json"
else
    echo "(no heartbeat — $DEMO_NAME has never run)"
fi

echo
echo "── Inspect restart history ──"
echo '$ cat '"$RUNTIME_DIR"'/restart.log'
if [[ -f "$RUNTIME_DIR/restart.log" ]]; then
    tail -10 "$RUNTIME_DIR/restart.log"
else
    echo "(no restart.log — agent has never been restarted)"
fi

echo
echo "── Force a restart to observe the policy (with --apply) ──"
echo '$ sac agents start '"$DEMO_NAME"
echo '$ tmux kill-session -t '"$DEMO_NAME"'   # simulate a crash'
echo '$ tail -f '"$RUNTIME_DIR"'/restart.log  # watch sac react'
echo '$ sac agents health '"$DEMO_NAME"
echo '  # → status: alive | unhealthy | dead'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agents health $DEMO_NAME (real) ──"
    sac agents health "$DEMO_NAME" || true
fi

# EOF
