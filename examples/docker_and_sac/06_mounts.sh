#!/usr/bin/env bash
# Lesson 06 — Bind mounts.
#
# Pure docker:
#   docker run -v /host/path:/container/path:ro <image>
#   docker run -v $HOME:$HOME:rw          <image>     # full passthrough
#
# sac equivalent — declared in spec.yaml, not on the command line:
#
#   spec:
#     mounts:
#       - src: ${HOME}/proj
#         dst: ${HOME}/proj
#         mode: rw
#       - src: ${HOME}/.config/secrets
#         dst: /run/secrets
#         mode: ro
#
# Why sac doesn't expose -v on the CLI:
#   Mounts are part of the agent's contract. Recording them in
#   spec.yaml lets the same agent be reproduced anywhere (other host,
#   different user) without remembering a long docker invocation.
#   ${VAR} expansion happens at start time using the host shell's env.
#
# Common patterns:
#   - dst same as src         → simplest; preserves absolute paths
#   - mode: ro                → read-only mounts for credentials
#   - mode: rw                → writable mounts for workspace dirs
set -euo pipefail

echo "── docker -v examples (not run) ──"
# shellcheck disable=SC2016 # intentional: $HOME/$PWD are literal in the printed example
echo '$ docker run -v "$HOME":"$HOME":rw  scitex-agent-container:sdk-persistent ...'
# shellcheck disable=SC2016
echo '$ docker run -v "$PWD":/workdir:ro scitex-agent-container:sdk-persistent ...'

echo
echo "── sac equivalent (spec.yaml fragment) ──"
cat <<'YAML'
spec:
  mounts:
    - src: ${HOME}
      dst: ${HOME}
      mode: rw
YAML
