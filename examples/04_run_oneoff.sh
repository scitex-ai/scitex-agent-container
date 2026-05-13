#!/usr/bin/env bash
# Lesson 04 — One-off exec vs. long-living agent; sending follow-up turns.
#
# What problem does this solve?
#   You need to decide: is this a "run a script once and exit" job, or
#   a "long-lived agent that takes follow-up prompts" job? Apptainer
#   distinguishes the two cleanly. sac is built for the latter case
#   but uses the former under the hood.
#
# Failure mode if you skip this:
#   - You wrap apptainer in a while-loop to fake long-lived behaviour
#     and end up reinventing instance management badly (no logs, no
#     graceful shutdown, no health probes).
#   - Or you use `instance start` for one-off work and forget to stop
#     it; the instance hangs around and shows up in `instance list`
#     until the next reboot.
#
# Apptainer has THREE ways to run:
#
#   apptainer exec  my.sif  python -c 'print("hi")'   # one-off, no startup hook
#   apptainer run   my.sif                            # one-off, runs %runscript
#   apptainer instance start my.sif myname            # long-living, daemonized
#                                                       (logs to ~/.apptainer/instances/logs)
#
# Notes for HPC users:
#   - There is no "daemon" — exec/run launch the process directly.
#   - The container runs as YOU (no -u flag, no fakeroot needed).
#   - $HOME is auto-mounted by default (use --no-home to opt out).
#
# sac equivalent:
#   sac uses `instance start`-style under the hood. The flow is:
#
#       sac agents start <name>                # boot a long-living agent
#       sac agents send  <name> "<prompt>"     # follow-up turn
#       sac agents tail  <name> --json         # watch session.jsonl
#       sac agents stop  <name>                # graceful shutdown
#
#   You never invoke apptainer directly — sac materialises the workspace
#   (workdir + dot_claude/) then dispatches `apptainer instance start`.
#   sac is apptainer-only since 2026-05-13 (no --runtime flag).
#
# Pure-apptainer way to fake "send a turn":
#   There isn't one. The Claude SDK keeps the session in-process; you
#   would have to manage stdin yourself. This is exactly the gap sac
#   fills — `sac agents send` writes to the SDK's input channel and
#   the response flows out via session.jsonl.
set -euo pipefail
APPLY="${1:-}"

# Top-level symlink in the dir-per-image layout points at the active SIF.
SIF="$HOME/.scitex/agent-container/containers/sac-scitex.sif"
NAME="${SAC_DEMO_AGENT:-hello-agent}"

echo "── (A) One-off: apptainer exec ──"
echo '$ apptainer exec '"$SIF"' python -c "print(\"hi from sif\")"'
echo '  # → hi from sif'
if [[ "$APPLY" == "--apply" && -f "$SIF" ]]; then
    apptainer exec "$SIF" python -c 'print("hi from sif")'
else
    echo "(dry-run; pass --apply, requires SIF built)"
fi

echo
echo "── (B) Long-living: sac agents start + send + tail ──"
echo '$ sac agents start '"$NAME"
echo '  # → started in background; PID + heartbeat written to runtime/<name>/'
echo '$ sac agents send  '"$NAME"' "What is 2 + 2?"'
echo '  # → 4'
echo '$ sac agents tail  '"$NAME"' --json -n 5'
echo '  # → last 5 JSONL records: user turn, assistant turn, tool_use, result'
echo '$ sac agents stop  '"$NAME"
echo '  # → SIGTERM, escalates to SIGKILL after 5s'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac agents start $NAME (real) ──"
    sac agents start "$NAME" || true
    echo
    echo "── sac agents send $NAME (real) ──"
    sac agents send "$NAME" "What is 2 + 2? Reply with the digit only." || true
    sleep 3
    echo
    echo "── sac agents tail $NAME --json -n 5 (real) ──"
    sac agents tail "$NAME" --json -n 5 || true
    echo
    echo "── sac agents stop $NAME (real) ──"
    sac agents stop "$NAME" || true
fi

# EOF
