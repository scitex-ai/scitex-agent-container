#!/usr/bin/env bash
# Lesson 05 — Stopping a long-living instance.
#
# Pure apptainer:
#   apptainer instance stop <name>         # SIGTERM, then SIGKILL after 10s
#   apptainer instance stop --all          # stop every instance YOU own
#   apptainer instance stop --signal SIGINT <name>
#
# Notes:
#   - There's no "remove" step like `docker rm`. An instance either
#     exists (running) or it doesn't. Stop = remove.
#   - SIF files on disk are independent — stopping doesn't delete
#     anything. Remove the SIF with plain `rm` if you want.
#
# sac equivalent:
#   sac agent stop <name>                  # graceful: SDK quit-turn
#                                            then runtime-specific stop
#   sac agent stop a b c                   # multiple
#   sac agent stop <name> --force          # tolerate stale state
set -euo pipefail
APPLY="${1:-}"
NAME="${SAC_DEMO_AGENT:-demo-noop}"

echo "── what apptainer would do ──"
echo '$ apptainer instance stop '"$NAME"

echo
echo "── what sac would do ──"
echo '$ sac agent stop '"$NAME"

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agent stop $NAME (real) ──"
    sac agent stop "$NAME" || true
fi
