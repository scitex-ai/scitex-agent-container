#!/usr/bin/env bash
# Lesson 05 — Listing running instances.
#
# Pure apptainer:
#   apptainer instance list             # per-user view
#   apptainer instance list --json
#
# Note: instances are scoped to YOUR user account on this node.
# There's no system-wide registry. Each HPC login node has its own
# view; if you launched on a compute node, the login node sees nothing.
#
# sac equivalent:
#   sac agents list                     # fleet view of registered agents
#                                        (apptainer-only; sac dropped
#                                         docker/podman on 2026-05-13)
#
# Locations in `sac agents list` use host@host-workdir:container-workdir
# (e.g. ywata-note-win@/tmp:/work), not the old "LOCAL" label.
set -euo pipefail

echo "── apptainer instance list ──"
apptainer instance list || true

echo
echo "── sac agents list ──"
sac agents list || true
