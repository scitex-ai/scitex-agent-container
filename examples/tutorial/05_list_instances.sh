#!/usr/bin/env bash
# Lesson 05 — Listing running instances vs. listing registered agents.
#
# What problem does this solve?
#   "What's running right now on this machine?" is the most common
#   troubleshooting question. There are two layers to ask it at:
#   the apptainer process layer (what containers are alive?) and the
#   sac registry layer (what agents has this host been told about,
#   regardless of running state?).
#
# Failure mode if you skip this:
#   - You SSH around looking for stale instances by hand.
#   - You start the same agent twice because `sac agents list` showed
#     it as "not running here" — but it was running on a different login
#     node. `sac --on <peer> agents list` is the cross-host fix
#     (see lesson 14).
#
# Pure apptainer:
#   apptainer instance list             # per-user view
#   apptainer instance list --json      # machine-readable
#   # → INSTANCE NAME    PID      IP    IMAGE
#   # → hello-agent      12345          /home/me/.scitex/.../sac-base.sif
#
# Note: instances are scoped to YOUR user account on this node.
# There's no system-wide registry. Each HPC login node has its own
# view; if you launched on a compute node, the login node sees nothing.
#
# sac equivalent:
#   sac agents list                     # fleet view of registered agents
#   sac agents list --priority          # ordered by priority label
#   sac agents list <name> --snapshot   # one-shot state dump
#   # → NAME           STATUS    LOCATION                     IMAGE
#   # → hello-agent    running   ywata-note-win@/tmp:/work    sac-base.sif
#
# Locations use host@host-workdir:container-workdir
# (e.g. ywata-note-win@/tmp:/work), not the old "LOCAL" label.
#
# Key difference: `apptainer instance list` shows what's RUNNING NOW.
# `sac agents list` shows what's REGISTERED — including stopped agents
# whose spec.yaml is still on disk. That distinction matters for
# `sac agents start <name>` (registered) vs. `sac agents delete <name>`
# (removes the registry entry).
set -euo pipefail

echo "── (A) apptainer instance list — process-level view ──"
echo '$ apptainer instance list'
echo '  # → empty table if nothing is running'
apptainer instance list 2>/dev/null || echo "(apptainer not installed or no instances)"

echo
echo "── (B) sac agents list — registry-level view ──"
echo '$ sac agents list'
echo '  # → all agents whose spec.yaml is under ~/.scitex/agent-container/agents/'
sac agents list 2>/dev/null || echo "(sac not installed or no agents registered)"

echo
echo "── (C) JSON forms (machine-readable, for orchestrators) ──"
echo '$ apptainer instance list --json | jq ".instances[].instance"'
echo '$ sac agents list --json'

# EOF
