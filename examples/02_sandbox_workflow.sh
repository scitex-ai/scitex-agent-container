#!/usr/bin/env bash
# Lesson 02 — The "scitex updates often, do we rebuild?" workflow.
#
# Problem:
#   scitex packages release frequently. Baking a version into a SIF
#   means rebuilding (60-90 min on cold cache, since `pip install
#   scitex[all]` pulls the full scientific stack) every time you want
#   fresh.
#
# Solution: apptainer's *sandbox* mode + sac image verbs.
#
# Dir-per-image layout (current):
#   ~/.scitex/agent-container/containers/
#     sac-base/sac-base.sif                # versioned :base SIF
#     sac-scitex/sac-scitex.sif            # versioned :scitex SIF
#     sac-scitex/sac-scitex.sandbox/       # writable sandbox dir
#     sac-base.sif    -> sac-base/sac-base.sif       # top-level symlink
#     sac-scitex.sif  -> sac-scitex/sac-scitex.sif   # top-level symlink
#
#   1. Build a writable sandbox once:
#        sac image build scitex --sandbox
#        # → ~/.scitex/agent-container/containers/sac-scitex/sac-scitex.sandbox/
#
#   2. Refresh packages any time:
#        sac image update ~/.scitex/agent-container/containers/sac-scitex/sac-scitex.sandbox/
#        sac image update ~/.scitex/agent-container/containers/sac-scitex/sac-scitex.sandbox/ -p scitex-dsp
#
#   3. When stable, freeze back to an immutable SIF:
#        sac image freeze ~/.scitex/agent-container/containers/sac-scitex/sac-scitex.sandbox/ \
#                         ~/.scitex/agent-container/containers/sac-scitex/sac-scitex-2.28.15.sif
#
#   4. Versioned switch / rollback handles the rest:
#        sac image list                  # see installed SIFs
#        sac image switch 2.28.15        # atomic flip
#        sac image rollback              # restore previous
#
# Pure apptainer equivalents (what sac image verbs delegate to):
#
#   apptainer build --sandbox sandbox/ apptainer-scitex.def
#   apptainer exec --writable sandbox/ pip install --upgrade scitex[all]
#   apptainer build out.sif sandbox/      # re-bake to immutable
#
# When to use sandbox:
#   ✓ Daily development; trying out new package versions
#   ✗ Production / CI / cross-machine reproducibility
#     (a sandbox can drift; SIF is byte-identical wherever you copy it)
set -euo pipefail
APPLY="${1:-}"

# User-state location for built artifacts (sandboxes + SIFs).
# Dir-per-image layout: sac-<layer>/sac-<layer>.{sif,sandbox}
CONTAINERS_DIR="$HOME/.scitex/agent-container/containers"
mkdir -p "$CONTAINERS_DIR"
SANDBOX_DIR="$CONTAINERS_DIR/sac-scitex/sac-scitex.sandbox"

echo "── existing sandbox (if any) ──"
ls -ld "$SANDBOX_DIR" 2>/dev/null || echo "(no sandbox yet)"

echo
echo "── sac image verbs (dry-run; pass --apply to execute) ──"
echo '$ sac image build scitex --sandbox'
echo '$ sac image update '"$SANDBOX_DIR"
echo '$ sac image freeze '"$SANDBOX_DIR"' sac-scitex-NEW.sif'

if [[ "$APPLY" == "--apply" ]]; then
    if [[ ! -d "$SANDBOX_DIR" ]]; then
        echo
        echo "── sac image build scitex --sandbox -y (real, 60-90 min — scitex[all] is heavy) ──"
        sac image build scitex --sandbox -y
    fi
    echo
    echo "── sac image update $SANDBOX_DIR (real) ──"
    sac image update "$SANDBOX_DIR"
fi
