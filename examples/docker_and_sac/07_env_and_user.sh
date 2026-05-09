#!/usr/bin/env bash
# Lesson 07 — Environment variables and the run-as user.
#
# Pure docker:
#   docker run -e FOO=bar -e API_KEY=$API_KEY <image>
#   docker run -u $(id -u):$(id -g) <image>   # run as host user
#   docker run --user 1000:1000 <image>
#
# sac equivalent — declared in spec.yaml; ${VAR} expanded at start time:
#
#   spec:
#     user: host                       # special: ${UID}:${GID} of host
#     env:
#       FOO: bar
#       API_KEY: ${API_KEY}            # forwarded from host shell
#       HOME: ${HOME}
#
# spec.user accepts:
#   ""           — image default (usually root)
#   "host"       — current host UID:GID (sac expands at start time)
#   "1000:1000"  — explicit numeric pair
#
# Why "host" matters:
#   Files written by the agent inside a bind-mount appear on the host
#   owned by whoever the container ran as. If that's root, you can't
#   `rm` them without sudo. `user: host` keeps file ownership sane.
set -euo pipefail

echo "── docker -e / -u examples (not run) ──"
# shellcheck disable=SC2016
echo '$ docker run -e FOO=bar -u "$(id -u):$(id -g)" scitex-agent-container:sdk-persistent ...'

echo
echo "── sac equivalent (spec.yaml fragment) ──"
cat <<'YAML'
spec:
  user: host
  env:
    FOO: bar
    API_KEY: ${API_KEY}
    HOME: ${HOME}
YAML
