#!/usr/bin/env bash
# Lesson 04 — Running a one-off command vs. starting a long-living instance.
#
# Apptainer has THREE ways to run:
#
#   apptainer exec  my.sif  python -c 'print("hi")'   # one-off, no startup hook
#   apptainer run   my.sif                            # one-off, runs %runscript
#   apptainer instance start my.sif myname            # long-living, daemonized
#
# Notes for HPC users:
#   - There is no "daemon" — exec/run launch the process directly.
#   - The container runs as YOU (no -u flag, no fakeroot needed).
#   - $HOME is auto-mounted by default (use --no-home to opt out).
#
# sac equivalent:
#   sac uses `instance start`-style under the hood for apptainer agents.
#   You don't run apptainer directly; sac materialises the workspace
#   then dispatches via apptainer (the only supported runtime since
#   the 2026-05-13 docker/podman ripout).
set -euo pipefail
APPLY="${1:-}"
# Top-level symlink in the dir-per-image layout points at the active SIF.
SIF="$HOME/.scitex/agent-container/containers/sac-scitex.sif"

echo "── apptainer exec (one-off) ──"
echo '$ apptainer exec '"$SIF"' python -c "print(\"hi from sif\")"'
if [[ "$APPLY" == "--apply" && -f "$SIF" ]]; then
    apptainer exec "$SIF" python -c 'print("hi from sif")'
else
    echo "(dry-run; pass --apply, requires SIF built)"
fi
