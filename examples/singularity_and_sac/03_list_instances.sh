#!/usr/bin/env bash
# Lesson 03 — Listing running instances.
#
# Pure apptainer:
#   apptainer instance list             # like `docker ps`, but per-user
#   apptainer instance list --json
#
# Note: instances are scoped to YOUR user account on this node.
# There's no system-wide registry. Each HPC login node has its own
# view; if you launched on a compute node, the login node sees nothing.
#
# sac equivalent:
#   sac agent status                    # tracks agents across runtimes
#                                        (docker, apptainer, slurm)
set -euo pipefail

echo "── apptainer instance list ──"
apptainer instance list || true

echo
echo "── sac agent status (cross-runtime) ──"
sac agent status
