#!/usr/bin/env bash
# Install the repo-local pre-push lint gate.
#
# Points git at .githooks/ (instead of .git/hooks/) so the hook lives
# under version control and travels with the clone. Run once per checkout.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath .githooks
echo "Installed pre-push hook: .githooks/pre-push"
echo "Verify with: git config --get core.hooksPath  (expect: .githooks)"
