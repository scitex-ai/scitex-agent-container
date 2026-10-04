#!/usr/bin/env bash
# Source-only helpers for provisioned CI scratch. A producer records the
# directory incarnation, job identity and current process/group birth.
# Normal cleanup is the exact job step or the waiting host supervisor.
# Startup prune needs an owned, quiescent group; age alone grants nothing.
# Legacy/malformed records and unknown bare-job completion remain untouched.
# Parent/leaf symlinks are refused and removals are time bounded.

_ci_tmpdir_root() {
    if [ -n "${SAC_CI_TMPDIR_ROOT:-}" ]; then
        printf '%s' "$SAC_CI_TMPDIR_ROOT"
        return 0
    fi

    local runner_tmp="${TMPDIR:-}" user_name="${USER:-}"
    case "$runner_tmp" in
    /tmp/?* | /var/tmp/?*)
        if [ -d "$runner_tmp" ] && [ -w "$runner_tmp" ]; then
            printf '%s' "$runner_tmp"
            return 0
        fi
        ;;
    esac

    [ -n "$user_name" ] || user_name="$(id -un 2>/dev/null)" || return 1
    if [ -d "/scratch/$user_name" ] && [ -w "/scratch/$user_name" ]; then
        printf '%s' "/scratch/$user_name/sac-ci/github-actions"
        return 0
    fi
    if [ -d "/data/gpfs/projects/punim0264" ] && [ -w "/data/gpfs/projects/punim0264" ]; then
        printf '%s' "/data/gpfs/projects/punim0264/$user_name/ci/job-scratch"
        return 0
    fi
    return 1
}

# The inner scripts and bare-runner workflow scopes that create per-run scratch,
# and the prefix each uses.
#
# THIS TABLE IS TESTED AGAINST THE SCRIPTS THEMSELVES
# (tests/integration/test_ci_tmpdir_lifecycle.py): a new *-in-sif.sh that
# exports a per-run TMPDIR and is not listed here fails CI, rather than quietly
# leaking 2 GB per run for four months like the three above it did.
ci_tmpdir_prefix_for_inner() {
    case "${1:-}" in
    run-in-sif.sh) printf 'ci' ;;
    build-in-sif.sh) printf 'build' ;;
    publish-in-sif.sh) printf 'publish' ;;
    docs) printf 'docs' ;;
    import-smoke) printf 'import' ;;
    lint) printf 'lint' ;;
    runner-guard) printf 'guard' ;;
    *) printf '' ;; # creates no per-run scratch
    esac
}

# The canonical per-run scratch name is unchanged. A historical directory
# without a complete owned incarnation is retained, even when its name matches.
ci_tmpdir_path() {
    local prefix="${1:?prefix required (ci|build|publish)}"
    local version="${2:?python version required}"
    printf '%s/%s-scitex_agent_container-%s-%s-%s' \
        "$(_ci_tmpdir_root)" "$prefix" \
        "${GITHUB_RUN_ID:-0}" "${GITHUB_RUN_ATTEMPT:-0}" "$version"
}

# Is this path one WE created, directly under the scratch root?
#
# The guard exists because ci_tmpdir_cleanup's argument is built from a
# workflow-interpolated matrix value and then handed to `rm -rf`. Rejects the
# root itself, anything outside it, anything nested deeper, and any traversal.
_ci_tmpdir_is_managed() {
    local d="${1:-}" root base
    root="$(_ci_tmpdir_root)"
    [ -n "$d" ] || return 1
    case "$d" in
    "$root"/*) ;;
    *) return 1 ;;
    esac
    base="${d#"$root"/}"
    case "$base" in
    '' | */* | *..*) return 1 ;;
    esac
    case "$base" in
    ci-scitex_agent_container-?* | build-scitex_agent_container-?* | publish-scitex_agent_container-?* | docs-scitex_agent_container-?* | import-scitex_agent_container-?* | lint-scitex_agent_container-?* | guard-scitex_agent_container-?*)
        return 0
        ;;
    esac
    return 1
}


# Ownership is recorded by the actual producer. Age selects candidates only.
# Unknown legacy directories are retained rather than treated as permission.
_ci_tmpdir_pid_start() {
    local row rest
    case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
    IFS= read -r row < "/proc/$1/stat" || return 1
    rest="${row##*) }"
    set -- $rest
    printf '%s' "${20}"
}

_ci_tmpdir_group_alive() {
    local wanted="${1:?group required}" rows member parent group state extra
    local live=0 dead=0 unknown=0
    kill -0 -- "-$wanted" 2>/dev/null || return 1
    # A zombie cannot execute work or hold scratch. Direct children are waited
    # by their supervisor; adopted nonchild reaping belongs to the runner.
    rows="$(timeout --signal=TERM --kill-after=1s 3s ps -eo pid=,ppid=,pgid=,stat=)" || {
        echo '::warning::owned group process metadata unknown; scratch retained' >&2
        return 0
    }
    while read -r member parent group state extra; do
        [ "$group" = "$wanted" ] || continue
        case "$member:$parent" in *[!0-9:]*) unknown=$((unknown+1)); continue ;; esac
        case "$state" in
            Z*|X*) dead=$((dead+1)) ;;
            R*|S*|D*|T*|t*|I*|W*) live=$((live+1)) ;;
            *) unknown=$((unknown+1)) ;;
        esac
    done <<< "$rows"
    echo "ci-tmpdir: remaining group members live=$live dead/nonchild=$dead unknown=$unknown (nonchild reaping unverified)" >&2
    [ "$live" -gt 0 ] || [ "$unknown" -gt 0 ]
}

_ci_tmpdir_root_safe() {
    local root
    root="$(_ci_tmpdir_root)" || return 1
    [ -d "$root" ] && [ ! -L "$root" ] && [ "$(realpath -e -- "$root")" = "$root" ]
}

ci_tmpdir_prepare() {
    local d="${1:-}" uid dev inode mode boot pid start group group_start kind job
    _ci_tmpdir_is_managed "$d" && _ci_tmpdir_root_safe || return 1
    [ ! -e "$d" ] && [ ! -L "$d" ] || {
        echo '::error::CI scratch already exists; refusing overwrite' >&2; return 1;
    }
    uid="$(id -u)"
    job="${GITHUB_JOB:-none}"
    case "$job" in ''|*[!a-zA-Z0-9_-]*) return 1 ;; esac
    pid="${SAC_CI_OWNER_PID:-$BASHPID}"
    start="$(_ci_tmpdir_pid_start "$pid")" || return 1
    [ "$start" = "${SAC_CI_OWNER_START:-$start}" ] || return 1
    group="${SAC_CI_GROUP_PID:-0}"; group_start=0; kind=job
    if [ "$group" != 0 ]; then
        group_start="$(_ci_tmpdir_pid_start "$group")" || return 1
        [ "$group_start" = "${SAC_CI_GROUP_START:-$group_start}" ] || return 1
        kind=group
    fi
    IFS= read -r boot < /proc/sys/kernel/random/boot_id || return 1
    mkdir -m 700 -- "$d" || return 1
    read -r dev inode mode < <(stat -c '%d %i %a' -- "$d")
    [ "$mode" = 700 ] || return 1
    (umask 077; set -o noclobber; printf '1 %s %s %s %s %s %s %s %s %s %s %s %s\n' \
        "$uid" "$dev" "$inode" "$boot" "$pid" "$start" "$group" "$group_start" \
        "$kind" "${GITHUB_RUN_ID:-0}" "${GITHUB_RUN_ATTEMPT:-0}" "$job" > "$d/.sac-ci-owner")
}

_ci_tmpdir_owned() {
    local d="${1:-}" marker schema uid dev inode boot pid start group group_start kind run attempt job extra now owner live
    _ci_tmpdir_is_managed "$d" && _ci_tmpdir_root_safe || return 1
    [ -d "$d" ] && [ ! -L "$d" ] && [ "$(realpath -e -- "$d")" = "$d" ] || return 1
    marker="$d/.sac-ci-owner"
    [ -f "$marker" ] && [ ! -L "$marker" ] || return 1
    [ "$(stat -c '%u:%a' -- "$marker")" = "$(id -u):600" ] || return 1
    [ "$(wc -l < "$marker")" = 1 ] || return 1
    read -r schema uid dev inode boot pid start group group_start kind run attempt job extra < "$marker" || return 1
    [ "$schema" = 1 ] && [ -z "$extra" ] && \
        [ "$(stat -c '%u:%d:%i:%a' -- "$d")" = "$uid:$dev:$inode:700" ] && [ "$uid" = "$(id -u)" ] || return 1
    case "$pid:$start:$group:$group_start:$run:$attempt" in *[!0-9:]*) return 1 ;; esac
    [ "$pid" -gt 0 ] && [ "$start" -gt 0 ] || return 1
    IFS= read -r now < /proc/sys/kernel/random/boot_id || return 1
    owner="$(_ci_tmpdir_pid_start "$pid" 2>/dev/null)" || owner=''
    if [ "$boot" = "$now" ] && [ "$owner" = "$start" ] && [ "$pid" != "$BASHPID" ]; then
        return 1
    fi
    case "$kind" in
    group)
        [ "$group" -gt 0 ] && [ "$group_start" -gt 0 ] || return 1
        if [ "$boot" = "$now" ] && _ci_tmpdir_group_alive "$group"; then return 1; fi
        ;;
    job)
        # A bare prepare step ends before its work step. No stale-job inference:
        # only the same job's explicit always-cleanup may reclaim it.
        [ "${2:-explicit}" != prune ] && [ "$group" = 0 ] && \
            [ "$run" = "${GITHUB_RUN_ID:-0}" ] && [ "$attempt" = "${GITHUB_RUN_ATTEMPT:-0}" ] && \
            [ "$job" = "${GITHUB_JOB:-none}" ] || return 1
        ;;
    *) return 1 ;;
    esac
}

ci_tmpdir_cleanup() {
    local d="${1:-}" before
    _ci_tmpdir_is_managed "$d" || {
        echo "::error::refusing to remove '$d' — not a managed CI scratch path" >&2; return 1;
    }
    [ -e "$d" ] || { [ ! -L "$d" ]; return; }
    _ci_tmpdir_owned "$d" "${2:-explicit}" || {
        echo '::warning::refusing to remove unproven or live CI scratch' >&2; return 1;
    }
    before="$(stat -c '%u:%d:%i:%a' -- "$d")"
    # Recheck the complete owner and directory incarnation immediately before rm.
    _ci_tmpdir_owned "$d" "${2:-explicit}" && [ "$(stat -c '%u:%d:%i:%a' -- "$d")" = "$before" ] || return 1
    # Restore owned directory search/write bits before rm can remove the owner
    # record. Traversal is physical and restricted to this filesystem.
    timeout --signal=TERM --kill-after=2s 30s find -P "$d" -xdev -type d -uid "$(id -u)" -exec chmod u+rwX -- '{}' + 2>/dev/null || return 1
    _ci_tmpdir_owned "$d" "${2:-explicit}" || return 1
    timeout --signal=TERM --kill-after=2s 30s rm -rf --one-file-system -- "$d" 2>/dev/null || return 1
    [ ! -e "$d" ] && [ ! -L "$d" ]
}

ci_tmpdir_prune() {
    local root age_h age_min run_id attempt d
    root="$(_ci_tmpdir_root)" || return 0
    _ci_tmpdir_root_safe || return 0
    age_h="${SAC_CI_TMPDIR_MAX_AGE_H:-24}"
    case "$age_h" in ''|*[!0-9]*) age_h=24 ;; esac
    [ "$age_h" -ge 1 ] && [ "$age_h" -le 8760 ] || age_h=24
    age_min=$((age_h * 60)); run_id="${GITHUB_RUN_ID:-0}"; attempt="${GITHUB_RUN_ATTEMPT:-0}"
    while IFS= read -r -d '' d; do
        if _ci_tmpdir_owned "$d" prune; then
            if ci_tmpdir_cleanup "$d" prune; then
                echo "ci-tmpdir: pruning leftover scratch (owned and quiescent): $d"
            fi
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d \
        ! -name "*-${run_id}-${attempt}-*" -mmin "+${age_min}" -print0 2>/dev/null)
    return 0
}
