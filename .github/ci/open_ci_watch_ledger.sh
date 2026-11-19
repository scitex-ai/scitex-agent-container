#!/usr/bin/env bash
# Open the ci-watch ledger branch as a worktree at DIR (default: ledger).
#
# Run from the root of an actions/checkout of this repository: the worktree
# shares that checkout's .git, so its persisted credentials push the row.
#
#   branch exists  -> DIR is a detached worktree at origin/ci-watch-ledger
#   branch absent  -> DIR is an empty orphan worktree; the first push creates
#                     the branch (it was never created, which is why every
#                     scheduled ci-watch run failed at checkout)
#   remote unreadable -> exit non-zero. An unreadable remote is NOT an absent
#                     branch: starting a fresh orphan there would fork the
#                     ledger's history.
set -euo pipefail

dir="${1:-ledger}"
branch="ci-watch-ledger"

set +e
git ls-remote --exit-code --heads origin "$branch" >/dev/null
rc=$?
set -e

case "$rc" in
0)
    git fetch --no-tags --depth=50 origin \
        "+refs/heads/$branch:refs/remotes/origin/$branch"
    git worktree add --detach "$dir" "origin/$branch"
    echo "ledger: opened existing $branch at $dir"
    ;;
2)
    git worktree add --detach "$dir" HEAD
    git -C "$dir" checkout -q --orphan "$branch-bootstrap"
    # -f: on an orphan every inherited path reads as "staged", so a plain
    # `git rm` refuses and the develop tree would leak into the ledger.
    git -C "$dir" rm -rfq --ignore-unmatch .
    git -C "$dir" clean -fdxq
    echo "ledger: $branch does not exist yet; started an orphan at $dir"
    ;;
*)
    echo "::error::cannot read origin heads (git ls-remote rc=$rc); refusing to guess whether $branch exists"
    exit 1
    ;;
esac
