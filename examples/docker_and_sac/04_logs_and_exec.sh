#!/usr/bin/env bash
# Lesson 04 — Reading transcripts and entering a running agent.
#
# Pure docker:
#   docker logs <name>                 # all stdout/stderr
#   docker logs --tail 50 -f <name>    # last 50 lines, follow
#   docker exec -it <name> bash        # interactive shell inside
#
# sac equivalent — speaks Claude SDK's session.jsonl, NOT raw stdout:
#   sac agent tail <name>              # recent assistant turns + tools
#   sac agent tail <name> -n 100       # last 100 turns
#   sac agent recall <name>            # human summary of the whole session
#
# Why the divergence:
#   Claude SDK writes structured records to session.jsonl. Tailing raw
#   docker stdout shows mostly framework noise; tailing session.jsonl
#   shows the conversation. sac picks the latter by default.
#
# To still get docker stdout (debug):
#   docker logs $(docker ps -q --filter "label=sac.agent.name=<name>")
set -euo pipefail

NAME="${SAC_DEMO_AGENT:-orchestrator}"

echo "── sac agent tail $NAME (last 5 turns) ──"
sac agent tail "$NAME" -n 5 || echo "(no transcript — agent not started?)"
