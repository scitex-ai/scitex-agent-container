#!/bin/bash
# PreToolUse guard for Bash wait loops that make `pgrep -f` match their own
# `bash -c` command line. The Python sibling owns parsing and diagnostics.

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE="$THIS_DIR/self_matching_pgrep_wait.py"

# Fail open if an incomplete deployment omitted the decision engine. A broken
# hook must not disable every Bash tool call.
[[ -f "$CORE" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

exec python3 "$CORE"

# EOF
