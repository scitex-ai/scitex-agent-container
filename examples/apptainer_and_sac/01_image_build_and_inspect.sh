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
#   apptainer build out.sif <pkg>/containers/apptainer-base.def     # build :base SIF
#   apptainer build out.sif <pkg>/containers/apptainer-scitex.def   # build :scitex SIF
#   apptainer inspect out.sif                                       # labels, def
#
# (Recipes ship inside the pip wheel at
#  <site-packages>/scitex_agent_container/containers/; resolve via
#  `python -c "import scitex_agent_container; print(scitex_agent_container.__file__)"`.)
#
# sac wrapper (delegates to scitex-container for versioning):
#   sac image build base                                      # build :base
#   sac image build scitex                                    # build :scitex (default)
#   sac image build scitex --sandbox                          # writable sandbox
#   sac image list                                            # versions on disk
#   sac image status                                          # unified dashboard
set -euo pipefail
APPLY="${1:-}"

# Built SIFs and sandboxes live in user state, not the repo. This is
# scitex's standard local-state convention (~/.scitex/<pkg>/...).
CONTAINERS_DIR="$HOME/.scitex/agent-container/containers"
mkdir -p "$CONTAINERS_DIR"

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
    echo "── sac image build base -y (real, ~15-25 min) ──"
    # :base = OS + dev tools + node + rust toolchain + cargo binstall'd
    # CLIs + tree-from-source + npm globals + chrome-headless-shell.
    # First build can be ~25 min on cold cache; subsequent builds reuse
    # the apt/cargo/npm caches and are faster.
    sac image build base -y
    echo
    echo "── sac image build scitex -y (real, 60-90 min — scitex[all] is heavy) ──"
    # :scitex layers `pip install scitex[all]` on top — pulls numpy /
    # pandas / scipy / torch / and the rest of the SciTeX scientific
    # stack. Genuinely an hour or more on a cold pip cache.
    sac image build scitex -y
fi
