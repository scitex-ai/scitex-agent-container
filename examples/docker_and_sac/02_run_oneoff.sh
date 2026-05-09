#!/usr/bin/env bash
# Lesson 02 — Running a one-off container vs. starting an agent.
#
# Pure docker — short-lived, exits when the command returns:
#   docker run --rm scitex-agent-container:sdk-persistent python -c 'print("hi")'
#
# Why sac doesn't do this:
#   sac agents are *long-living* — Claude SDK runtime keeps a session
#   alive so subsequent turns reuse the same context. The closest
#   analogue is `sac agent start`, but that needs a spec.yaml.
#
# sac equivalent (long-living):
#   sac agent start <name>      # detached, container persists
#   sac agent stop  <name>
set -euo pipefail
APPLY="${1:-}"

echo "── docker run --rm (one-off, prints inside container) ──"
echo '$ docker run --rm scitex-agent-container:sdk-persistent python -c "print(\"hi from container\")"'
if [[ "$APPLY" == "--apply" ]]; then
    docker run --rm scitex-agent-container:sdk-persistent python -c 'print("hi from container")'
else
    echo "(dry-run; pass --apply to actually execute)"
fi
