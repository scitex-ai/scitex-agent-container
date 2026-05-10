#!/usr/bin/env bash
# Lesson 01 — Building and inspecting a SIF image.
#
# Each agent picks its image in spec.yaml — same as docker:
#
#   spec:
#     runtime: apptainer
#     image: /path/to/scitex-agent-container.sif      # default-ish
#   # or:
#     image: /scratch/${USER}/cuda-agent.sif          # GPU SIF
#     image: my-custom.sif                            # bring your own
#
# `sac image build --runtime apptainer` builds the *bundled default*
# SIF (containers/scitex-agent-container.sif). Custom SIFs you build
# with plain apptainer.
#
# Singularity/Apptainer images are *single-file* (.sif), not layered
# like docker. There's no daemon, no registry pull by default — the
# build is reproducible from a definition file (apptainer.def).
#
# Pure apptainer:
#   apptainer build my.sif apptainer.def              # def file → SIF
#   apptainer build my.sif docker://ubuntu:24.04      # convert from docker
#   apptainer inspect my.sif                          # labels, def file
#   apptainer inspect --runscript my.sif              # what `run` would do
#
# sac wrapper (default SIF only):
#   sac image build --runtime apptainer               # uses containers/apptainer.def
#                                                       → containers/scitex-agent-container.sif
#   sac image build --runtime apptainer --dry-run
set -euo pipefail
APPLY="${1:-}"

SIF=/home/ywatanabe/proj/scitex-agent-container/containers/scitex-agent-container.sif

echo "── existing SIF (if any) ──"
ls -la "$SIF" 2>/dev/null || echo "(no SIF built yet)"

echo
echo "── sac image build --runtime apptainer --dry-run ──"
sac image build --runtime apptainer --dry-run

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac image build --runtime apptainer (real, can take 5-10 min) ──"
    sac image build --runtime apptainer -y
    echo
    echo "── apptainer inspect ──"
    apptainer inspect "$SIF"
fi
