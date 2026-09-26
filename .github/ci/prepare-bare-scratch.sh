#!/usr/bin/env bash
set -euo pipefail

SCOPE="${1:?managed scratch scope required}"
VERSION="${2:?runtime version required}"
VENV_KEY="${3:?venv environment key required}"
case "$VENV_KEY" in
'' | *[!A-Z0-9_]*)
    echo "::error::invalid venv environment key '$VENV_KEY'" >&2
    exit 1
    ;;
esac

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$CI_DIR/tmpdir-lib.sh"

PREFIX="$(ci_tmpdir_prefix_for_inner "$SCOPE")"
if [ -z "$PREFIX" ]; then
    echo "::error::unknown managed scratch scope '$SCOPE'" >&2
    exit 1
fi
SCRATCH="$(ci_tmpdir_path "$PREFIX" "$VERSION")"
ci_tmpdir_cleanup "$SCRATCH"
ci_tmpdir_prune
mkdir -p "$SCRATCH/tmp" "$SCRATCH/uv-cache"
chmod 700 "$SCRATCH" "$SCRATCH/tmp" "$SCRATCH/uv-cache"

{
    echo "TMPDIR=$SCRATCH/tmp"
    echo "UV_CACHE_DIR=$SCRATCH/uv-cache"
    echo "$VENV_KEY=$SCRATCH/venv"
} >> "${GITHUB_ENV:?GITHUB_ENV is required}"
