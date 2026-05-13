#!/usr/bin/env bash
# Lesson 07 — Stopping vs. removing: what "stop" actually does.
#
# What problem does this solve?
#   You need to bring an agent down cleanly. "Cleanly" has three layers
#   that are easy to confuse:
#     1. Tell Claude to finish its current turn  (SDK quit-turn)
#     2. Send SIGTERM to the apptainer instance  (process-level stop)
#     3. Delete the registry entry / state dir   (forget it ever ran)
#   sac collapses (1) + (2) into `sac agents stop`. Removing the spec
#   from the registry is a separate `sac agents delete`.
#
# Failure mode if you skip this:
#   - You `kill -9` the agent, lose the last partial turn, and leave
#     orphaned files in runtime/<name>/ (lockfile, pid). Next start
#     refuses because state looks live. Recover with --force.
#   - You assume `sac agents stop` deletes the registry entry. It does
#     not. `sac agents list` will still show the agent (status: stopped).
#     Use `sac agents delete <name> -y` to actually forget it.
#
# Pure apptainer:
#   apptainer instance stop <name>            # SIGTERM, then SIGKILL after 10s
#   apptainer instance stop --all             # stop every instance YOU own
#   apptainer instance stop --signal SIGINT <name>
#   apptainer instance stop --timeout 30 <name>   # custom grace period
#
# Notes:
#   - There's no separate "remove" step in apptainer. An instance
#     either exists (running) or it doesn't. Stop = remove from
#     `instance list`.
#   - SIF files on disk are independent — stopping doesn't delete
#     anything. Remove the SIF with plain `rm` if you want.
#
# sac equivalent:
#   sac agents stop <name>                    # graceful: SDK quit-turn then SIGTERM
#   sac agents stop a b c                     # multiple in parallel
#   sac agents stop <name> --force            # tolerate stale state files
#   sac agents stop --all                     # every registered, running agent
#   sac agents delete <name> -y               # remove registry entry + state dir
#
# Three-stage shutdown sequence sac follows:
#   1. POST a "quit" turn to the SDK so any in-flight tool call has a
#      chance to drain. Up to ~5s.
#   2. `apptainer instance stop` — SIGTERM, escalate to SIGKILL after 5s.
#   3. Clean up runtime/<name>/{pid,heartbeat.json,lockfile}.
#
# If (1) hangs (Claude stuck on permission prompt etc.), `--force`
# skips straight to (2). State files are blown away regardless.
set -euo pipefail
APPLY="${1:-}"
NAME="${SAC_DEMO_AGENT:-hello-agent}"

echo "── (A) Stop one instance ──"
echo '$ apptainer instance stop '"$NAME"
echo '  # → INFO: Stopping '"$NAME"' instance of /path/to/sac-base.sif (PID=...)'
echo '$ sac agents stop '"$NAME"
echo '  # → quit-turn sent; instance stopped; state files cleaned'

echo
echo "── (B) Stop all (panic button) ──"
echo '$ apptainer instance stop --all'
echo '$ sac agents stop --all'

echo
echo "── (C) Force-stop a wedged agent ──"
echo '$ sac agents stop '"$NAME"' --force'
echo '  # → skips SDK quit-turn; SIGTERM straight to apptainer'

echo
echo "── (D) Actually forget the agent (registry-level delete) ──"
echo '$ sac agents delete '"$NAME"' -y'
echo '  # → removes ~/.scitex/agent-container/agents/'"$NAME"'/ and runtime/'"$NAME"'/'
echo '  # → spec.yaml in source dir is NOT touched'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agents stop $NAME (real) ──"
    sac agents stop "$NAME" || true
fi

# EOF
