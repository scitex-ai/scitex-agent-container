#!/usr/bin/env bash
# Commit FILE in the ledger worktree DIR and push it to ci-watch-ledger.
#
# usage: push_ci_watch_ledger.sh DIR FILE MESSAGE
#
# The poll and host-load legs of one run append to different files, so a
# rejected push is resolved by rebasing onto the branch tip and retrying.
# The rebase also covers the first run, when both legs start an orphan and
# only one of them can create the branch. A push that still fails after the
# retries is an error: a row that never reached the ledger is a stalled
# watcher, and the run says so.
set -euo pipefail

dir="$1"
file="$2"
message="$3"
branch="ci-watch-ledger"

cd "$dir"
git config user.name "ci-watch"
git config user.email "ci-watch@scitex.ai"
git add -- "$file"
if git diff --cached --quiet; then
    echo "ledger: no new row in $file"
    exit 0
fi
git commit -q -m "$message"

for attempt in 1 2 3 4; do
    if git push -q origin "HEAD:refs/heads/$branch"; then
        echo "ledger: pushed $file (attempt $attempt)"
        exit 0
    fi
    sleep $((attempt * 5))
    git fetch --no-tags --depth=50 origin \
        "+refs/heads/$branch:refs/remotes/origin/$branch"
    if ! git rebase -q "origin/$branch"; then
        git rebase --abort
        echo "::error::ledger rebase onto origin/$branch conflicted for $file"
        exit 1
    fi
done
echo "::error::could not push the $file row to $branch after 4 attempts"
exit 1
