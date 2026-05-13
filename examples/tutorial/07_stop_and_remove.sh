#!/usr/bin/env bash
# Lesson 07 — Stopping a long-living instance.
#
# Pure apptainer:
#   apptainer instance stop <name>         # SIGTERM, then SIGKILL after 10s
#   apptainer instance stop --all          # stop every instance YOU own
#   apptainer instance stop --signal SIGINT <name>
#
# Notes:
#   - There's no separate "remove" step. An instance either exists
#     (running) or it doesn't. Stop = remove.
#   - SIF files on disk are independent — stopping doesn't delete
#     anything. Remove the SIF with plain `rm` if you want.
#
# sac equivalent:
#   sac agents stop <name>                 # graceful: SDK quit-turn
#                                            then runtime-specific stop
#   sac agents stop a b c                  # multiple
#   sac agents stop <name> --force         # tolerate stale state
set -euo pipefail
APPLY="${1:-}"
NAME="${SAC_DEMO_AGENT:-demo-noop}"

echo "── what apptainer would do ──"
echo '$ apptainer instance stop '"$NAME"

echo
echo "── what sac would do ──"
echo '$ sac agents stop '"$NAME"

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agents stop $NAME (real) ──"
    sac agents stop "$NAME" || true
fi
