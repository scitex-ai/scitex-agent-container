#!/usr/bin/env bash
# Lesson 03 — Versioned SIFs: list / switch / rollback / snapshot.
#
# After freezing sandboxes into SIFs (see Lesson 02), you accumulate
# multiple versioned images:
#
#   ~/.scitex/agent-container/containers/
#     scitex-agent-container-scitex.sif         → currently active (symlink)
#     scitex-agent-container-scitex-2.28.14.sif
#     scitex-agent-container-scitex-2.28.15.sif
#     ...
#
# sac image (delegates to scitex-container's atomic-symlink versioning):
#
#   sac image list                  # show all installed versions
#   sac image switch 2.28.15        # atomic flip; previous remembered
#   sac image rollback              # restore the previous version
#   sac image status                # unified dashboard
#   sac image snapshot              # capture pip + apt + git → JSON
#
# Why atomic:
#   `switch_version()` moves a symlink in one rename(2). Agents starting
#   mid-flip see either the old or the new SIF, never a half-state.
#
# Reproducibility capsule:
#   `sac image snapshot` writes a single JSON with pip freeze + apt list
#   + git commits + active SIF hash. Attach to a paper / share with
#   a collaborator → they can rebuild the exact env.
set -euo pipefail
# Read-only lesson — no --apply branch needed.

echo "── sac image list ──"
sac image list || true

echo
echo "── sac image status ──"
sac image status || true

echo
echo "── sac image snapshot (preview, first 30 lines) ──"
sac image snapshot 2>/dev/null | head -30 || echo "(snapshot unavailable in this env)"

echo
echo "── examples (dry-run only) ──"
echo '$ sac image switch 2.28.15'
echo '$ sac image rollback'
echo '$ sac image snapshot -o env.json'
