#!/usr/bin/env bash
# Run every example top-to-bottom. Outputs land under examples/_out/.
set -euo pipefail

cd "$(dirname "$0")"

for ex in 01_list_running_agents.py 02_mcp_self_introspect.py; do
    echo "── $ex ────────────────────────────────────────"
    python "$ex"
done

echo "── done ──"
