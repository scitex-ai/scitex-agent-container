#!/usr/bin/env bash
# Run-scoped storage for the SIF-based CI jobs. Source this file; it has no
# side effects until a function is called.

_ci_safe_component() {
    local value="${1:-unknown}"
    value="${value//[^A-Za-z0-9_.-]/-}"
    value="${value#-}"
    value="${value%-}"
    printf '%s' "${value:-unknown}"
}

_ci_username() {
    _ci_safe_component "${USER:-$(id -un 2>/dev/null || printf unknown)}"
}

ci_tmpdir_resolve_root() {
    if [ -n "${SAC_CI_TMPDIR_ROOT:-}" ]; then
        printf '%s' "$SAC_CI_TMPDIR_ROOT"
        return 0
    fi

    local user scratch_base candidate fallback_base
    user="$(_ci_username)"
    scratch_base="${SAC_CI_SCRATCH_BASE:-/scratch}"
    candidate="$scratch_base/$user/scitex-agent-container/ci"
    if [ -d "$scratch_base" ] && mkdir -p -- "$candidate" 2>/dev/null && [ -w "$candidate" ]; then
        printf '%s' "$candidate"
        return 0
    fi

    fallback_base="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
    candidate="$fallback_base/scitex-agent-container-ci-$user"
    mkdir -p -- "$candidate" || return 1
    [ -w "$candidate" ] || return 1
    printf '%s' "$candidate"
}

_ci_tmpdir_root() {
    ci_tmpdir_resolve_root
}

ci_tmpdir_prefix_for_inner() {
    case "${1:-}" in
    run-in-sif.sh) printf 'ci' ;;
    build-in-sif.sh) printf 'build' ;;
    publish-in-sif.sh) printf 'publish' ;;
    *) printf '' ;;
    esac
}

# Exact directory owned by one workflow job/matrix leg. Every disposable store
# is a child: work (pytest/pip/uv), postgres, and apptainer.
ci_tmpdir_path() {
    local prefix version run_id attempt suffix root
    prefix="$(_ci_safe_component "${1:?prefix required}")"
    version="$(_ci_safe_component "${2:?version required}")"
    run_id="$(_ci_safe_component "${GITHUB_RUN_ID:-manual}")"
    attempt="$(_ci_safe_component "${GITHUB_RUN_ATTEMPT:-0}")"
    suffix="${prefix}-scitex_agent_container-${run_id}-${attempt}-${version}"
    if [ -z "${GITHUB_RUN_ID:-}" ]; then
        suffix="${suffix}-$$"
    fi
    root="$(_ci_tmpdir_root)" || return 1
    printf '%s/%s' "$root" "$suffix"
}

ci_work_tmpdir_path() {
    printf '%s/work' "${SAC_CI_RUN_DIR:-$(ci_tmpdir_path "$1" "$2")}"
}

_ci_tmpdir_is_managed() {
    local d="${1:-}" root base
    root="$(_ci_tmpdir_root)" || return 1
    [ -n "$d" ] || return 1
    case "$d" in "$root"/*) ;; *) return 1 ;; esac
    base="${d#"$root"/}"
    case "$base" in
    ?*/* | *..*) return 1 ;;
    ci-scitex_agent_container-?* | build-scitex_agent_container-?* | publish-scitex_agent_container-?* | exec-scitex_agent_container-?*) return 0 ;;
    *) return 1 ;;
    esac
}

ci_tmpdir_cleanup() {
    local d="${1:-}"
    if ! _ci_tmpdir_is_managed "$d"; then
        echo "::error::refusing to remove '$d' — not a managed CI run directory" >&2
        return 1
    fi
    [ -e "$d" ] || return 0
    rm -rf -- "$d" 2>/dev/null && return 0
    chmod -R u+rwX -- "$d" 2>/dev/null || true
    rm -rf -- "$d" 2>/dev/null || true
    [ ! -e "$d" ]
}

ci_tmpdir_log() {
    local d="${1:?run directory required}" free output _fs _blocks _used available _rest
    free="unknown"
    if output="$(df -Pk -- "$d" 2>/dev/null)"; then
        while read -r _fs _blocks _used available _rest; do
            case "$available" in '' | *[!0-9]*) continue ;; esac
            free="$available KiB"
        done <<<"$output"
    fi
    echo "ci-run-storage: root=$d free=$free"
}

# SIGKILL cannot be trapped, so the next run removes only old run namespaces.
ci_tmpdir_prune() {
    local root age_h age_min run_id attempt d
    root="$(_ci_tmpdir_root)" || return 0
    [ -d "$root" ] || return 0
    age_h="${SAC_CI_TMPDIR_MAX_AGE_H:-24}"
    case "$age_h" in '' | *[!0-9]*) age_h=24 ;; esac
    [ "$age_h" -ge 1 ] 2>/dev/null || age_h=24
    age_min=$((age_h * 60))
    run_id="$(_ci_safe_component "${GITHUB_RUN_ID:-manual}")"
    attempt="$(_ci_safe_component "${GITHUB_RUN_ATTEMPT:-0}")"
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        echo "ci-run-storage: pruning leftover scratch older than ${age_h}h: $d"
        ci_tmpdir_cleanup "$d" || true
    done < <(
        find "$root" -mindepth 1 -maxdepth 1 -type d \
            \( -name 'ci-scitex_agent_container-*' -o -name 'build-scitex_agent_container-*' -o -name 'publish-scitex_agent_container-*' -o -name 'exec-scitex_agent_container-*' \) \
            ! -name "*-${run_id}-${attempt}-*" \
            -mmin "+${age_min}" \
            -print 2>/dev/null || true
    )
    return 0
}
