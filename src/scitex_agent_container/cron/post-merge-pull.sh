#!/usr/bin/env bash
# post-merge-pull.sh — fast-forward every scitex-* in-tree clone from its
# tracked upstream (origin/develop on the fleet checkouts).
#
# Designed to run under cron once a minute on each fleet host so the host
# checkouts stay current and nobody has to pull by hand.
#
# Discovery (fixes the stale-checkout incident, card
# sac-auto-pull-broken-manual-pulls-20260630): every directory matching
# ~/proj/scitex  and  ~/proj/scitex-*  is considered — NOT a hand-maintained
# allowlist. A hardcoded list silently omitted repos (scitex-dev, scitex-todo,
# …) so they went stale while merged PRs sat unpulled; glob discovery keeps
# the sweep complete as the fleet grows. Non-git dirs (tarballs, `*.worktrees`
# container dirs), feature-branch checkouts and dirty trees are filtered out by
# the per-repo guards below, so the wide glob is safe.
#
# Remote-agnostic: uses each branch's configured upstream — no hardcoded
# remote name or URL.  Only ever fast-forwards a checkout that is on `develop`
# with a clean working tree; feature-branch / dirty / ahead / diverged
# checkouts are left untouched (never clobbers unpushed local work).
#
# Fail-loud but resilient: one problem repo never aborts the sweep. A repo
# that is behind its upstream but cannot be fast-forwarded (missing upstream,
# unreachable remote, racing ff) is logged as a WARN and makes the whole run
# exit non-zero, so the failure is visible in cron's log rather than silently
# skipped-and-forgotten. Expected non-actionable states (ahead / diverged /
# dirty / not-on-develop) are logged but do not fail the run.
#
# Usage:
#   crontab: * * * * * ~/.scitex/agent-container/runtime/cron/post-merge-pull.sh \
#              >> ~/.scitex/agent-container/runtime/logs/post-merge-pull.$(hostname -s).cron.log 2>&1

set -euo pipefail
shopt -s nullglob

HOST="$(hostname -s)"
PROJ_DIR="${HOME}/proj"
RUNTIME_DIR="${HOME}/.scitex/agent-container/runtime"
LOG_DIR="${RUNTIME_DIR}/logs"
LOG_FILE="${LOG_DIR}/post-merge-pull.${HOST}.log"
# Lock lives under HOME (per-host, per-user) rather than a shared /tmp path so
# concurrent users on a multi-tenant host — and hermetic test runs with an
# isolated $HOME — never collide on a single global lock.
LOCK_FILE="${RUNTIME_DIR}/post-merge-pull.${HOST}.lock"

mkdir -p "${LOG_DIR}"

_ts() { date '+%Y-%m-%dT%H:%M:%S'; }
_info() { echo "$(_ts) [INFO] $*" | tee -a "${LOG_FILE}"; }
_warn() { echo "$(_ts) [WARN] $*" | tee -a "${LOG_FILE}" >&2; }

# Repos that were behind but could not be fast-forwarded (real failures).
SWEEP_FAILURES=()
_record_failure() { SWEEP_FAILURES+=("$1"); }

# Acquire exclusive lock — abort if another run is still active.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    _warn "Another instance is running (${LOCK_FILE}). Exiting."
    exit 0
fi

# ---------------------------------------------------------------------------
# Repo discovery: every ~/proj/scitex and ~/proj/scitex-* directory. The wide
# net is deliberate — the per-repo guards in _pull_repo (must be a git repo,
# on develop, clean, with a tracked upstream) filter out everything that
# should not be touched, so no hand-maintained allowlist can drift stale.
# ---------------------------------------------------------------------------
_discover_repos() {
    [[ -d "${PROJ_DIR}" ]] || return 0
    local d
    for d in "${PROJ_DIR}"/scitex "${PROJ_DIR}"/scitex-*; do
        [[ -d "${d}" ]] || continue
        printf '%s\n' "${d}"
    done
}

_pull_repo() {
    local repo_path="$1"
    local name
    name="$(basename "${repo_path}")"

    # Must be an actual git repo (skips tarballs, `*.worktrees` container dirs).
    if ! git -C "${repo_path}" rev-parse --git-dir &>/dev/null; then
        _info "SKIP ${name}: not a git repository"
        return 0
    fi

    # Only ever touch a checkout that is on develop — never disturb a
    # feature-branch checkout.
    local branch
    branch="$(git -C "${repo_path}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [[ "${branch}" != "develop" ]]; then
        _info "SKIP ${name}: on '${branch}', not 'develop'"
        return 0
    fi

    # Skip repos with uncommitted local changes (contributor workspace guard).
    local dirty
    dirty="$(git -C "${repo_path}" status --porcelain 2>/dev/null || true)"
    if [[ -n "${dirty}" ]]; then
        _warn "SKIP ${name}: has uncommitted changes — not pulling"
        return 0
    fi

    # Resolve the branch's tracked upstream (e.g. origin/develop). No upstream
    # means this checkout is not wired for auto-pull — log loudly so it is not
    # silently forgotten, but it is a config state, not a run failure.
    local upstream
    upstream="$(git -C "${repo_path}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [[ -z "${upstream}" ]]; then
        _warn "SKIP ${name}: no upstream configured for 'develop'"
        return 0
    fi
    local remote="${upstream%%/*}"

    # Refresh remote refs. A failed fetch (unreachable remote) on a checkout we
    # are meant to keep current IS a real failure — flag it.
    if ! git -C "${repo_path}" fetch --quiet "${remote}" 2>>"${LOG_FILE}"; then
        _warn "FAIL ${name}: git fetch ${remote} failed (unreachable remote?)"
        _record_failure "${name}"
        return 0
    fi

    local local_sha remote_sha base
    local_sha="$(git -C "${repo_path}" rev-parse HEAD)"
    remote_sha="$(git -C "${repo_path}" rev-parse "${upstream}")"
    base="$(git -C "${repo_path}" merge-base HEAD "${upstream}" 2>/dev/null || true)"

    if [[ "${local_sha}" == "${remote_sha}" ]]; then
        _info "OK ${name}: already at ${local_sha:0:12} (no new commits)"
        return 0
    fi

    if [[ "${base}" == "${remote_sha}" ]]; then
        # Local is ahead of upstream — unpushed local commits. Never clobber.
        _info "SKIP ${name}: local is ahead of ${upstream} (unpushed commits) — not pulling"
        return 0
    fi

    if [[ "${base}" != "${local_sha}" ]]; then
        # Neither ancestor of the other — histories diverged. Needs a human;
        # ff-only would fail, so leave it and surface it loudly.
        _warn "SKIP ${name}: diverged from ${upstream} — manual reconcile needed"
        return 0
    fi

    # base == local_sha and local != remote  ⇒  strictly behind ⇒ fast-forward.
    if git -C "${repo_path}" merge --ff-only "${upstream}" >>"${LOG_FILE}" 2>&1; then
        _info "OK ${name}: ${local_sha:0:12} → ${remote_sha:0:12}"
    else
        _warn "FAIL ${name}: fast-forward to ${upstream} failed"
        _record_failure "${name}"
    fi
    return 0
}

_info "--- post-merge-pull start (host=${HOST}, proj=${PROJ_DIR}) ---"

_repo_count=0
while IFS= read -r repo_path; do
    [[ -n "${repo_path}" ]] || continue
    _repo_count=$((_repo_count + 1))
    _pull_repo "${repo_path}"
done < <(_discover_repos)

if [[ ${#SWEEP_FAILURES[@]} -gt 0 ]]; then
    _warn "--- post-merge-pull done: ${#SWEEP_FAILURES[@]}/${_repo_count} repo(s) FAILED to pull: ${SWEEP_FAILURES[*]} ---"
    exit 1
fi
_info "--- post-merge-pull done: ${_repo_count} repo(s) checked, all clear ---"
