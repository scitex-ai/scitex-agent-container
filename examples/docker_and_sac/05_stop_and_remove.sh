#!/usr/bin/env bash
# Lesson 05 — Stopping and cleaning up.
#
# Pure docker:
#   docker stop <name>                # SIGTERM, wait, SIGKILL
#   docker rm <name>                  # remove stopped container
#   docker rm -f <name>               # stop + remove in one shot
#   docker container prune            # nuke every stopped container
#
# sac equivalent:
#   sac agent stop <name>             # graceful: sends quit-turn to SDK,
#                                       waits for clean exit, then docker stop
#   sac agent stop <name> --force     # tolerate stale registry / hook fail
#   sac agent stop <a> <b> <c>        # multiple
#
# What sac does extra over `docker stop`:
#   1. Notifies the SDK runner so the assistant gets a chance to checkpoint
#   2. Updates ~/.scitex/agent-container/state.db (registry) so
#      `sac agent status` reflects reality
#   3. Cleans up materialised workspace files (CLAUDE.md, .mcp.json, .env)
#      under runtime/<name>/<name>/
set -euo pipefail
APPLY="${1:-}"
NAME="${SAC_DEMO_AGENT:-demo-noop}"

echo "── what would docker do ──"
echo '$ docker stop '"$NAME"
echo '$ docker rm   '"$NAME"

echo
echo "── what sac would do ──"
echo '$ sac agent stop '"$NAME"

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agent stop $NAME (real) ──"
    sac agent stop "$NAME" || true
fi
