#!/usr/bin/env bash
# Point git at the VERSION-CONTROLLED hooks. Idempotent; run once per clone.
#
# Supersedes scripts/install-pre-push-hook.sh, which did the same `git config`
# and which nothing ever called: it was documented in CONTRIBUTING.md as a
# "one-time" manual step, and on this box — the box the whole fleet commits
# from — it had never been run. `core.hooksPath` still pointed at the ABSOLUTE
# `.git/hooks`, so BOTH advertised gates were dead:
#
#   * `.pre-commit-config.yaml`  — framework shim never installed
#   * `.githooks/pre-push`       — shadowed by .git/hooks
#
# What actually ran was an April-7 git-TEMPLATE hook. lint.yml's own comment
# admits the consequence: the CI ruff job exists because "a push that bypassed
# the local hook (--no-verify, missing ruff, fresh clone w/o
# scripts/install-pre-push-hook.sh) still fails on the remote". The local gate
# was assumed, not enforced.
#
# WHY .githooks/ AND NOT .git/hooks/: `.git/hooks` is untracked, so every fresh
# clone and every `git worktree add` silently starts with NO hooks and no way to
# know it. `.githooks/` is committed — it travels with the clone. This one line
# of config is the only thing that cannot travel, which is why it lives in a
# script instead of a README bullet nobody reads.
#
# NOTE ON WORKTREES: `core.hooksPath` lives in `.git/config`, which every
# worktree of this repo SHARES. Running this once therefore arms the hooks for
# the main checkout and for every agent worktree under .worktrees/ at the same
# time — which is the intent.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath .githooks

echo "core.hooksPath -> .githooks"
echo
echo "Now ACTIVE (and version-controlled):"
echo "  .githooks/pre-commit  -> pre-commit framework (fast, bounded checks;"
echo "                           NEVER the test suite - CI is the gate)"
echo "  .githooks/pre-push    -> ruff F401/F811 + protected-branch guard"
echo
echo "Verify with: git config --get core.hooksPath   (expect: .githooks)"
