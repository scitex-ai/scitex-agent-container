#!/usr/bin/env bash
# Lesson 04 — Reading logs and entering a running instance.
#
# Pure apptainer:
#   apptainer instance logs <name>            # combined stdout + stderr
#   apptainer instance logs <name> --err      # stderr only
#   apptainer exec instance://<name> bash     # interactive shell inside
#
# Logs land at:
#   ~/.apptainer/instances/logs/<host>/<user>/<name>.{out,err}
#
# sac equivalent:
#   sac agent tail <name>                     # session.jsonl, structured
#   sac agent recall <name>                   # session summary
#
# Why sac prefers session.jsonl over instance logs:
#   apptainer logs are raw stdout/stderr from the process. Claude SDK
#   writes structured JSON records to session.jsonl describing each
#   turn (user/assistant/tool_use/result). Tailing the structured
#   stream is far more useful for understanding what the agent is doing.
set -euo pipefail

NAME="${SAC_DEMO_AGENT:-orchestrator}"

echo "── sac agent tail $NAME ──"
sac agent tail "$NAME" -n 5 || echo "(no transcript)"
