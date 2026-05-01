#!/usr/bin/env bash
# post-merge-pull.sh — pull every scitex-* in-tree clone from gitea:develop.
#
# Designed to run under cron once a minute on each fleet host.
# Scope: ~/proj/<repo> only.  ~/forks/ and workspace clones are skipped.
#
# Usage:
#   crontab: * * * * * ~/.scitex/orochi/shared/cron/post-merge-pull.sh \
#              >> ~/.scitex/orochi/shared/logs/post-merge-pull.$(hostname -s).cron.log 2>&1

set -euo pipefail

HOST="$(hostname -s)"
LOG_DIR="${HOME}/.scitex/orochi/shared/logs"
LOG_FILE="${LOG_DIR}/post-merge-pull.${HOST}.log"
LOCK_FILE="/tmp/post-merge-pull.${HOST}.lock"

mkdir -p "${LOG_DIR}"

_ts() { date '+%Y-%m-%dT%H:%M:%S'; }
_info() { echo "$(_ts) [INFO] $*" | tee -a "${LOG_FILE}"; }
_warn() { echo "$(_ts) [WARN] $*" | tee -a "${LOG_FILE}" >&2; }

# Acquire exclusive lock — abort if another run is still active.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    _warn "Another instance is running (${LOCK_FILE}). Exiting."
    exit 0
fi

# ---------------------------------------------------------------------------
# Repo list: canonical name → gitea remote URL
# ---------------------------------------------------------------------------
declare -A GITEA_URLS=(
    ["scitex-agent-container"]="git@git.scitex.ai:ywatanabe1989/scitex-agent-container.git"
    ["scitex-orochi"]="git@git.scitex.ai:ywatanabe1989/scitex-orochi.git"
    ["scitex-resource"]="git@git.scitex.ai:ywatanabe1989/scitex-resource.git"
    ["scitex-ssh"]="git@git.scitex.ai:ywatanabe1989/scitex-ssh.git"
    ["scitex-hpc"]="git@git.scitex.ai:ywatanabe1989/scitex-hpc.git"
    ["scitex"]="git@git.scitex.ai:ywatanabe1989/scitex.git"
)

_pull_repo() {
    local name="$1"
    local repo_path="${HOME}/proj/${name}"

    [[ -d "${repo_path}" ]] || { _info "SKIP ${name}: not found at ${repo_path}"; return 0; }

    # Skip repos with uncommitted local changes (contributor workspace guard).
    local dirty
    dirty="$(git -C "${repo_path}" status --porcelain 2>/dev/null || true)"
    if [[ -n "${dirty}" ]]; then
        _warn "SKIP ${name}: has uncommitted changes — not pulling"
        return 0
    fi

    # Ensure gitea remote exists (idempotent).
    local gitea_url="${GITEA_URLS[${name}]}"
    if ! git -C "${repo_path}" remote get-url gitea &>/dev/null; then
        git -C "${repo_path}" remote add gitea "${gitea_url}"
        _info "Added remote 'gitea' → ${gitea_url} for ${name}"
    fi

    # Fetch + ff-only pull from gitea develop.
    if ! git -C "${repo_path}" fetch gitea develop 2>>"${LOG_FILE}"; then
        _warn "FAIL ${name}: fetch gitea develop failed"
        return 0
    fi

    local before_sha after_sha
    before_sha="$(git -C "${repo_path}" rev-parse HEAD)"

    if ! git -C "${repo_path}" pull --ff-only gitea develop 2>>"${LOG_FILE}"; then
        _warn "FAIL ${name}: pull --ff-only failed (non-fast-forward?)"
        return 0
    fi

    after_sha="$(git -C "${repo_path}" rev-parse HEAD)"
    if [[ "${before_sha}" == "${after_sha}" ]]; then
        _info "OK ${name}: already at $(echo "${after_sha}" | head -c 12) (no new commits)"
    else
        _info "OK ${name}: ${before_sha:0:12} → ${after_sha:0:12}"
    fi
}

_info "--- post-merge-pull start (host=${HOST}) ---"
for repo_name in "${!GITEA_URLS[@]}"; do
    _pull_repo "${repo_name}"
done
_info "--- post-merge-pull done ---"
