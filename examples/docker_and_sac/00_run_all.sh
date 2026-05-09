#!/usr/bin/env bash
# Run every numbered docker-vs-sac demo top-to-bottom.
# Each demo prints what it would run, then runs the read-only ones for real.
# Mutating ops (build/run/stop) are shown but commented unless --apply is set.
set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$THIS_DIR"

OUT_DIR="$THIS_DIR/_out"
mkdir -p "$OUT_DIR"

APPLY=""
[[ "${1:-}" == "--apply" ]] && APPLY="--apply"

for f in "$THIS_DIR"/0[1-9]_*.sh; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    echo
    echo "════════════════ $name ════════════════"
    bash "$f" $APPLY 2>&1 | tee "$OUT_DIR/${name%.sh}.log"
done

echo
echo "── done. logs under $OUT_DIR ──"
