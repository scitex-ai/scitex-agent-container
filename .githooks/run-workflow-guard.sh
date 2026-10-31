#!/usr/bin/env bash
# Use an explicitly selected, existing interpreter; never sync or create an env.
set -euo pipefail

case "${1:-}" in
  hosted) guard="_hosted_runner_guard.py" ;;
  pool) guard="_runner_pool_guard.py" ;;
  *) echo "WORKFLOW GUARD BLOCKED: expected hosted or pool." >&2; exit 2 ;;
esac
if [ "$#" -ne 1 ]; then
  echo "WORKFLOW GUARD BLOCKED: unexpected arguments." >&2
  exit 2
fi

UV="${SAC_HOOK_UV:-$(command -v uv || true)}"
PYTHON="${SAC_HOOK_PYTHON:-}"
for tool in "$UV" "$PYTHON"; do
  case "$tool" in
    /*) ;;
    *) echo "WORKFLOW GUARD BLOCKED: set absolute SAC_HOOK_UV and SAC_HOOK_PYTHON paths." >&2; exit 2 ;;
  esac
  if [ ! -f "$tool" ] || [ ! -x "$tool" ]; then
    echo "WORKFLOW GUARD BLOCKED: configured executable is unavailable." >&2
    exit 2
  fi
done

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# The guard imports PyYAML and scitex-logging, already declared by this project.
# Refuse a missing dependency in the selected environment; do not install one.
unset VIRTUAL_ENV CONDA_PREFIX PYTHONHOME PYTHONPATH PYTHONUSERBASE UV_PROJECT_ENVIRONMENT
exec "$UV" run --no-project --no-python-downloads --no-config --offline --no-index \
  --python "$PYTHON" "$ROOT/src/scitex_agent_container/$guard" "$ROOT"
