#!/usr/bin/env bash
# Lesson 01 — Building layered SIF images.
#
# Two layers, one default:
#   :base    OS + dev tools (git, gh, rust CLIs, mermaid, uv, pipx, ...)
#   :scitex  FROM :base + scitex[all] + claude-agent-sdk + sac
#            ← default image when spec.image is unset
#
# Each agent picks its image in spec.yaml:
#
#   spec:
#     runtime: apptainer
#     image: /path/to/scitex-agent-container-scitex.sif    # :scitex (default)
#   # or:
#     image: /path/to/scitex-agent-container-base.sif      # bare-metal layer
#     image: /scratch/${USER}/cuda-agent.sif               # custom GPU SIF
#
# Apptainer images are *single-file* (.sif), no daemon, no registry pull
# by default — the build is reproducible from a definition file (.def).
#
# Pure apptainer:
#   apptainer build out.sif containers/apptainer-base.def     # build :base SIF
#   apptainer build out.sif containers/apptainer-scitex.def   # build :scitex SIF
#   apptainer inspect out.sif                                 # labels, def
#
# sac wrapper (delegates to scitex-container for versioning):
#   sac image build base                                      # build :base
#   sac image build scitex                                    # build :scitex (default)
#   sac image build scitex --sandbox                          # writable sandbox
#   sac image list                                            # versions on disk
#   sac image status                                          # unified dashboard
set -euo pipefail
APPLY="${1:-}"

CONTAINERS_DIR=/home/ywatanabe/proj/scitex-agent-container/containers

echo "── existing SIFs (if any) ──"
ls -la "$CONTAINERS_DIR"/*.sif 2>/dev/null || echo "(no SIFs built yet)"

echo
echo "── sac image list ──"
sac image list || true

echo
echo "── sac image status ──"
sac image status || true

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac image build base -y (real, ~10 min) ──"
    sac image build base -y
    echo
    echo "── sac image build scitex -y (real, ~10 min) ──"
    sac image build scitex -y
fi
