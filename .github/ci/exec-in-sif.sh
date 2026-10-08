#!/usr/bin/env bash
# Host-side, digest-fenced wrapper for the supported in-SIF drivers.
set -euo pipefail
INNER="${1:?inner script required}"; shift
case "$INNER" in ''|*/*|*..*) echo '::error::invalid inner script' >&2; exit 1 ;; esac
[ -f ".github/ci/$INNER" ] && [ ! -L ".github/ci/$INNER" ] || exit 1
V="${1:-3.12}"
if [ "$INNER" = run-in-sif.sh ]; then
    case "$V" in 3.11|3.12|3.13) ;; *) exit 1 ;; esac
    case "${2:-matrix}" in matrix|nightly) ;; *) echo '::error::unknown test suite profile' >&2; exit 1 ;; esac
fi
. "$(dirname "${BASH_SOURCE[0]}")/tmpdir-lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/sif-runtime-lib.sh"
SIF="${SCITEX_CI_SIF:?SCITEX_CI_SIF required}"; SIF="${SIF/#\~/$HOME}"
ci_sif_verify "$SIF" "${SCITEX_CI_SIF_SHA256:-}" || exit 1
APPTAINER_VAR="${SCITEX_CI_APPTAINER:-}"; APPTAINER_VAR="${APPTAINER_VAR/#\~/$HOME}"
if [ -n "$APPTAINER_VAR" ] && [ -x "$APPTAINER_VAR" ]; then
    APPTAINER="$APPTAINER_VAR"; APPTAINER_FROM=SCITEX_CI_APPTAINER
elif command -v apptainer >/dev/null 2>&1; then
    APPTAINER="$(command -v apptainer)"; APPTAINER_FROM=PATH
else
    echo '::error::no executable SCITEX_CI_APPTAINER or apptainer on PATH. Install apptainer on this runner.' >&2
    exit 1
fi
if [ "$INNER" = run-in-sif.sh ]; then
    CI_PG_SIF="${SCITEX_CI_PG_SIF:?SCITEX_CI_PG_SIF required for test driver}"
    CI_PG_SIF="${CI_PG_SIF/#\~/$HOME}"
    ci_sif_verify "$CI_PG_SIF" "${SCITEX_CI_PG_SIF_SHA256:-}" || exit 1
fi
SAC_CI_TMPDIR_ROOT="$(_ci_tmpdir_root)"; export SAC_CI_TMPDIR_ROOT
mkdir -p -- "$SAC_CI_TMPDIR_ROOT"
_ci_tmpdir_root_safe || exit 1
ci_tmpdir_prune
CI_RUNTIME="$(ci_tmpdir_path guard "sif-$INNER-$V")"
ci_tmpdir_prepare "$CI_RUNTIME"
CI_HOME="$CI_RUNTIME/home"; APPTAINER_TMPDIR="$CI_RUNTIME/apptainer-tmp"
mkdir -m 700 -- "$CI_HOME" "$APPTAINER_TMPDIR" "$CI_HOME/apptainer-cache"
CI_PG_PID=''; CI_PG_START=''; RUN_PID=''; RUN_START=''
ci_finish() {
    local status=$? safe=1 prefix target
    trap - EXIT INT TERM
    ci_stop_group "$RUN_PID" "$RUN_START" || safe=0
    ci_stop_group "$CI_PG_PID" "$CI_PG_START" || safe=0
    if [ "$safe" != 1 ] && [ "$status" = 0 ]; then status=1; fi
    if [ "$safe" = 1 ]; then
        prefix="$(ci_tmpdir_prefix_for_inner "$INNER")"
        if [ -n "$prefix" ]; then
            target="$(ci_tmpdir_path "$prefix" "$V")"
            ci_tmpdir_cleanup "$target" || echo '::warning::inner scratch retained' >&2
        fi
        ci_tmpdir_cleanup "$CI_RUNTIME" || echo '::warning::runtime scratch retained' >&2
    fi
    exit "$status"
}
trap ci_finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
set -m
ci_runtime_env
if [ "$INNER" = run-in-sif.sh ]; then
    CI_PG_ROOT="$CI_RUNTIME/postgres"; CI_PG_DATA="$CI_PG_ROOT/data"
    mkdir -m 700 -- "$CI_PG_ROOT"
    ci_pg_start || { echo '::error::required owned PG18 unavailable' >&2; exit 1; }
fi
APPTAINER_ARGV=(exec --cleanenv --contain --no-home --home "$CI_HOME" --pwd "$PWD" --bind "$PWD" --bind "$SAC_CI_TMPDIR_ROOT")
GPFS_PROJECT="/data/gpfs/projects/punim0264"
if [ -d "$GPFS_PROJECT" ]; then
    APPTAINER_ARGV+=(--bind "$GPFS_PROJECT")
    GPFS_STATE='present (punim0264 bound; test scratch resolved independently)'
else
    GPFS_STATE='absent (no GPFS bind; test scratch resolved independently)'
fi
ci_sif_verify "$SIF" "$SCITEX_CI_SIF_SHA256" || exit 1
if [ "$INNER" = run-in-sif.sh ]; then ci_sif_verify "$CI_PG_SIF" "$SCITEX_CI_PG_SIF_SHA256" || exit 1; fi
echo "exec-in-sif: apptainer=$APPTAINER (via $APPTAINER_FROM)"
echo "exec-in-sif: $GPFS_PROJECT $GPFS_STATE"
echo "exec-in-sif: verified image=$SIF own HOME=$CI_HOME scratch=$SAC_CI_TMPDIR_ROOT"
(
    set +m
    trap 'sleep 3' INT TERM
    group="$BASHPID"; birth="$(ci_pid_birth "$group")"
    # The birth record is mode-gated on read (owner 600): create it
    # restrictively regardless of the ambient umask, exactly like the
    # ownership marker in ci_tmpdir_prepare.
    (umask 077; printf '%s|%s\n' "$group" "$birth" > "$CI_RUNTIME/run.identity")
    CI_ENV+=("APPTAINERENV_SAC_CI_GROUP_PID=$group" "APPTAINERENV_SAC_CI_GROUP_START=$birth")
    ci_clean_exec "$APPTAINER" "${APPTAINER_ARGV[@]}" "$SIF" bash ".github/ci/$INNER" "$@" &
    child=$!; child_status=0; wait "$child" || child_status=$?; exit "$child_status"
) &
RUN_PID=$!; RUN_START="$(ci_group_birth_record "$RUN_PID" "$CI_RUNTIME/run.identity")" || exit 1
status=0
wait "$RUN_PID" || status=$?
exit "$status"
