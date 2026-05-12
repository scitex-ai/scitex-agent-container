#!/usr/bin/env bash
# Migrate ~/.scitex/agent-container/containers/ from the flat layout
# (one .sif + many .overlay.img at the top level) to the dir-per-image
# layout that mirrors scitex-template/templates/singularity/.
#
# Target shape:
#
#   containers/
#   ├── sac-base.sif -> sac-base/sac-base.sif
#   ├── sac-base/
#   │   ├── sac-base.sif
#   │   ├── sac-base.def
#   │   └── sac-base.build-YYYY-MMDD-HHMMSS.log
#   └── overlays/
#       └── proj-<pkg>.overlay.img
#
# Idempotent: safe to re-run. Reads SAC_CONTAINERS_DIR override if set,
# else defaults to ~/.scitex/agent-container/containers.

set -uo pipefail

CONTAINERS_DIR="${SAC_CONTAINERS_DIR:-$HOME/.scitex/agent-container/containers}"
DEF_SRC_DIR="${SAC_DEF_SRC_DIR:-$HOME/proj/scitex-agent-container/src/scitex_agent_container/containers}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '[migrate] %s\n' "$*"; }
# `run` joins all positional args with spaces and feeds the result to
# eval. Using "$*" (not "$@") sidesteps the array-vs-string footgun
# that linters warn about when eval mixes the two.
run() { if [ "$DRY_RUN" = 1 ]; then printf '  DRY: %s\n' "$*"; else eval "$*"; fi; }

[ -d "$CONTAINERS_DIR" ] || {
    log "no containers dir at $CONTAINERS_DIR — nothing to do"
    exit 0
}

cd "$CONTAINERS_DIR" || {
    log "cd $CONTAINERS_DIR failed"
    exit 1
}

# ---- 1. sac-base ----------------------------------------------------
# Accept either the new name (sac-base.sif) or the legacy name
# (scitex-agent-container-base.sif). After migration the canonical name
# is sac-base.
legacy_sif=""
for cand in sac-base.sif scitex-agent-container-base.sif; do
    if [ -f "$cand" ] && [ ! -L "$cand" ]; then
        legacy_sif="$cand"
        break
    fi
done

if [ -n "$legacy_sif" ]; then
    log "found flat SIF: $legacy_sif"
    run mkdir -p sac-base
    if [ ! -f sac-base/sac-base.sif ]; then
        run mv "$legacy_sif" sac-base/sac-base.sif
    else
        log "sac-base/sac-base.sif already exists — removing the flat copy"
        run rm -f "$legacy_sif"
    fi
    # Top-level convenience symlink — overwrite if it exists.
    run ln -sfn sac-base/sac-base.sif sac-base.sif
    # Drop a copy of the .def alongside the SIF for provenance.
    if [ -f "$DEF_SRC_DIR/apptainer-base.def" ] && [ ! -f sac-base/sac-base.def ]; then
        run cp "$DEF_SRC_DIR/apptainer-base.def" sac-base/sac-base.def
    fi
    # Best-effort: capture latest build log if a sibling file exists.
    if [ -f /tmp/sif-base-rebuild9.log ] && [ ! -f sac-base/sac-base.build-2026-0512-135800.log ]; then
        run cp /tmp/sif-base-rebuild9.log sac-base/sac-base.build-2026-0512-135800.log
    fi
fi

# ---- 2. overlays/ ---------------------------------------------------
shopt -s nullglob
overlays=(proj-*.overlay.img)
if [ "${#overlays[@]}" -gt 0 ]; then
    log "moving ${#overlays[@]} overlay files into overlays/"
    run mkdir -p overlays
    for ov in "${overlays[@]}"; do
        if [ ! -f "overlays/$ov" ]; then
            run mv "$ov" "overlays/$ov"
        else
            log "overlays/$ov already exists — removing duplicate flat copy"
            run rm -f "$ov"
        fi
    done
fi

# ---- 3. report ------------------------------------------------------
log "post-migration listing:"
find "$CONTAINERS_DIR" -mindepth 1 -maxdepth 1 -printf '%y %p\n' 2>/dev/null | sort | head -20
overlay_count=$(find "$CONTAINERS_DIR/overlays" -mindepth 1 -maxdepth 1 -type f 2>/dev/null | wc -l)
log "overlays/ count: $overlay_count"
log "done."
